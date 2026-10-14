# Stage 13 — Evals

**Status: complete. 19 cases, all green in 42.9s; the suite is proven able to fail a run. One
definition-of-done item — a live-model run — cannot be satisfied on this host and is recorded as
such below rather than described as done.**

The eval suite is the only gate in this repository that can fail while every other one passes,
because it is the only one that exercises the *assembly* rather than the parts. Stage 11 found by
hand the class of defect it exists for — an approved write recorded as `executed` when the backend
had refused it — and this stage's job was to make that class catchable by a command.

## What the plan asks for

- `eval/cases.json` — the 9 provided samples mapped to our tool names, plus our own: restart between
  propose and approve, approval expiry, memory across sessions, memory isolation between users,
  pagination page 2, not-found clarification, invalid enum handled, flaky-write idempotency,
  out-of-scope refusal, digression then resume, timeout gives up, secret never leaked.
- `eval/runner.py` driving the **real running service over HTTP**.
- Assertions for tool calls, writes-before-approval, ticket creation, duplicate prevention, output
  content, declines, completion, retries, and a latency budget.
- `results/latest.{md,json}`; a non-zero exit on failure; ADR-0005.

**Definition of done.** `make eval` green on the deterministic provider; a deliberately broken case
proven to fail the run; one live-model run recorded and committed; the results table in the README.

## The case inventory

Nineteen cases in eight families. Each carries a `why` that the report reproduces beside a failure,
so a red cell is never just a red cell.

| Family | Cases |
| --- | --- |
| right tool chosen | `right_tool_search`, `right_tool_read`, `noise_then_search`, `pagination_page_two` |
| approval step respected | `approval_respected`, `approval_declined_writes_nothing`, `invalid_enum_handled`, `digression_then_resume`, `approval_expiry`, `restart_between_propose_and_approve` |
| out-of-scope request refused | `out_of_scope_refused`, `first_contact` |
| a run with failures switched on | `failures_switch_on`, `not_found_clarification`, `failures_do_not_duplicate_a_write` |
| a run that runs out of time | `timeout_gives_up` |
| memory | `memory_across_sessions`, `memory_isolation_between_users` |
| the key never appears | `secret_never_leaked` |

All nine provided samples are present. Two are renamed to say what they check rather than which
sample they came from: the brief's "tool selection" is split into `right_tool_search` and
`right_tool_read`, and its "graceful failure" is `failures_switch_on`.

## Design decisions

**The suite drives the assembled service, never the internals.** `eval/stack.py` starts the mock and
the agent as real uvicorn servers on free ports and points the agent's MCP subprocess at the running
mock. Every assertion travels over HTTP. The only import of agent internals is `build_app`, which is
how the service is started.

**Cases are data.** A case is a list of steps and a list of assertions, both in `cases.json`, so the
file is the whole specification of what is measured and adding a case does not mean touching Python.
The scorer raises on an unknown assertion type rather than skipping it, and the runner raises on an
unknown *step* kind for the same reason: a typo that quietly asserted or did nothing would make the
suite greener and less true.

**One stack for the whole run, and one exception to it.** A fresh stack per case would hide state
that leaks between turns, which is a real defect class here — the approval cases write real tickets
and the memory cases accumulate real facts. But a turn budget and an approval TTL are read when the
application is built, so a case that needs a different one cannot be served by a running process at
all. Those cases declare `stack` overrides in `cases.json` and get a stack of their own, started and
stopped around them. `CASE_OVERRIDABLE_ENV` is the whole list of what may vary, and the loader
refuses anything else — so a case cannot appear to configure a setting that nothing reads.

**The settings cache is cleared before a second stack is built.** `build_app` reads the environment
once and caches it. Without the clear, a tighter budget set for one case would silently apply to the
next, and the suite would report a case as configured that it never configured. That failure is
invisible in a passing run, which is exactly why it has a function and a paragraph.

**The backend is the witness.** Ticket counts are read before the case, before the decision, and
after. Three readings, because "the proposing turn wrote nothing" and "the approving decision wrote
one" are different claims. `tool_not_executed` exists because a proposed write *is* recorded
deliberately, so `tool_not_called` is the wrong question about it.

**A refusal is an expected answer when the case says so.** `Stack.decide` takes a `tolerate` set of
status codes; a case that expects an expiry declares `"expect": "refused"` and gets a recorded
`refused` outcome instead of a raised error. Any status the case did not declare still raises, so
declaring an expectation can never hide a real fault behind it.

**The restart is a rebuilt application, not a new process.** `Stack.restart_agent` stops the agent,
disposes its engine, clears the settings cache and builds the whole application again — which
includes the lifespan, and with it the MCP client and its ticketing subprocess. What it does not
reproduce is the process id, and that is the honest limit: an approval that survives here survived a
rebuilt application reading a durable file. The store itself under a genuinely new process is
covered by the repository tests, which need no server.

**One rule in the scripted provider exists for its duration.** A turn deadline is a wall-clock
allowance, and the only honest way to exhaust one is for something inside the turn to take too long.
`slow_turn` waits two seconds and then searches, so the turn is cut short *while trying to do the
work*. Putting that wait in the mock instead would have made every case slower to prove one thing
about the loop.

**The run is hermetic.** The ticketing server's idempotency ledger defaults to a relative path, so
by default it outlives the in-memory mock — which makes the *second* run of the suite behave
differently from the first, and a case asserting one ticket fail for a reason unrelated to the agent.
`stack.py` points `TICKETING_LEDGER_PATH` at the run's own temporary directory.

**A failing case exits non-zero.** The suite is a gate, not a report.

## What was built

| Piece | Where | What it is |
| --- | --- | --- |
| The stack | `eval/stack.py` | Two real servers, a migrated throwaway database, a per-run ledger path, per-case environment overrides, and a restart that rebuilds the application |
| The runner | `eval/runner.py` | Loads and shape-checks cases, performs `invoke`/`decide`/`restart`/`sleep_s` steps, scores them, writes results, exits non-zero on failure |
| The scorer | `eval/scoring.py` | Thirteen assertion types, each with a sentence naming what was wanted and what happened |
| The cases | `eval/cases.json` | Nineteen cases across seven families |
| The report | `eval/report.py` | The Markdown table and the JSON payload |
| The suite's own tests | `tests/unit/test_eval_suite.py` | That the case file is well formed, that scoring detects contradiction, and that the loader rejects what it should |
| The design | `docs/adr/0005-behavioural-evaluation.md` | Why the suite is shaped this way |

## The defects the suite found

Each was found by running the thing, not by reading it. The first four were found during the stages
that built the cases; the fifth was found in this stage, while building the case that needed it.

1. **The replay flag was read from prose.** The write path decided `replayed` by searching the tool's
   text for the words "replayed" and "recovered". The ticketing server's sentence for the case it
   matters on is "Ticket TCK-1025 already existed for this request; nothing was created", which
   contains neither — so the flag read false and the decision told the caller a fresh ticket had been
   created while the detail beside it said nothing had. The flag is now read from the structured
   field the MCP layer had been sending all along.

2. **A refused call reached the response as a successful one.** The MCP adapter converted the
   client's result to text and dropped `is_error`, so the loop had no way to know a call had failed
   and reported every call as executed. A refused search reached the response as a successful one,
   with the only record of the refusal being the sentence fed to the model. `failures_switch_on` and
   `not_found_clarification` are what keep it fixed.

3. **A proposal was reported as a failed call.** `is_error` was derived as "anything that is not
   `executed`", which makes a write waiting for a human indistinguishable from one that broke. The
   loop now carries an explicit outcome vocabulary — `executed`, `failed`, `proposed`, `refused` —
   typed as the same literal the published contract uses.

4. **Cross-session memory had no caller.** `FactRepository` was written, migrated and fully tested in
   Stage 9, and nothing in the request path ever wrote a fact from a conversation or read one into a
   prompt. The assignment's "at least one cross-session fact" was met by a table and a repository
   rather than by the agent. The `memory_*` cases found it, because they ask the service a question
   over HTTP instead of asking the repository a question in Python.

5. **`AGENT_REQUEST_DEADLINE_S` was a documented setting that changed nothing.** Building
   `timeout_gives_up` needed a turn to actually run out of time, and it would not: the handler
   constructed its loop limits and omitted the turn budget, so every turn used the loop's own
   90-second default and the setting an operator would tune was decorative. This is the same defect
   class as the loader's override check — something that appears to configure behaviour and does not
   — and it is invisible in every passing test, because a turn that finishes in a millisecond cannot
   tell a 90-second budget from a 0.5-second one.

There was also a smaller one in this stage's own scorer: the `payloads_avoid` branch rebound
`offenders` — already a list of *tool calls* earlier in the same function — to a list of *indices*.
mypy saw it once the eval package was inside the type-checked file set; a reader would not have.

## Deviations from the plan, recorded rather than glossed

- **`eval/scoring.py`, not `eval/assertions.py`.** The module does more than hold assertions: it holds
  the `Step`/`Observation`/`CaseResult` vocabulary the runner and the report share, and it scores. The
  name follows the content.
- **Ticket counts, not a mock reset between cases.** The plan says "mock reset and per-case mock
  environment". The suite runs one stack and reads the store as the witness, which is strictly
  stronger: a case cannot pass because a reset papered over state the previous case left. Chaos *is*
  toggled per case, because the mock reads that flag per request. The one place a stack is rebuilt is
  a case that needs a different process setting, which no running server can be talked into.
- **`restart` and `sleep_s` are steps, and are not recorded as steps.** They produce no response to
  assert on, and recording them would shift every `turn` index in a case — so a case that restarts
  between a proposal and an answer would have to count the restart when naming the turn it means.
- **Assertion names are ours, not the brief's.** The brief's list named behaviour, not spellings:
  `no_write_before_approval` is `tool_not_executed`, `ticket_created` and `no_duplicate_tickets` are
  both `tickets_delta`, `output_contains_any` is `output_contains`, and `declines`/`completes` are
  `decision_outcome_is`/`stop_reason_is`. Every claim in the brief is asserted; none of them needed a
  second assertion type with a longer name.
- **No latency budget assertion.** The plan listed one. It is not here, and it is not here on purpose:
  a wall-clock assertion in a suite that starts two servers, a subprocess and a database measures the
  machine it runs on rather than the service, and a flaky threshold is a test people learn to ignore.
  `timeout_gives_up` asserts the thing that actually matters — that a turn which exceeds its budget
  stops and says so — and the run's per-case duration is recorded in the report for anyone watching
  for a regression.

## Verification

- `python -m eval` — 19 cases, 19 passed, 42.9s; `eval/results/latest.{md,json}` written and
  committed.
- `uv run python -m devtasks test-unit` — 376 passed, 4 deselected (the integration marker).
- `uv run python -m devtasks lint` — clean; `format` — 132 files unchanged; `typecheck` — mypy
  strict, no issues in 100 source files.
- The deliberately-broken fixture, run end to end:

  ```
  uv run python -m eval --cases eval/fixtures/deliberately-broken.json \
      --results eval/results/_broken_proof --quiet
  ```

  exits **1**, and the report it writes names both failures — `tool_called`: "expected a
  create_ticket call, saw 'search_tickets'" and `tickets_delta`: "expected the backend to gain 1
  ticket(s), it gained 0" — beside the case's own reason for existing. The output directory is not
  committed; the fixture it scores is, and a unit test scores the same fixture in-process.

## The one definition-of-done item that is not met

**A live-model run was not recorded, because there is no model credential on this host.** `.env`
carries no `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or Bedrock region, and `Settings` reads only the
process environment (`env_file=None` is deliberate: compose and the shell own the environment), so
there is nowhere for a key to come from. The run is therefore not recorded, and it is not described
as though it were.

The command, for a machine that has one:

```sh
AGENT_MODEL_PROVIDER=anthropic ANTHROPIC_API_KEY=... AGENT_MODEL_ID=claude-... \
    uv run python -m eval
```

Two things about that run are worth stating before someone attempts it. First, the eval's
deterministic mode is the `scripted` provider *by design* (ADR-0002): the cases assert specific tool
choices and specific arguments, so a real model is expected to fail several of them — the value of
the live run is that it exercises the loop against a provider that was not written alongside the
suite, not that it goes green. Second, `right_tool_read`'s `tool_argument_is TCK-1002` and
`pagination_page_two`'s `page=2` are assertions about *our* prompt-engineering, not about the
model's correctness, so a failure there says something different from a failure on
`approval_respected`.

## Carried forward

- The live-model run above is the only unmet item, and it needs a credential rather than work.
- `eval/` is inside the mypy and ruff file sets, so the suite's own code is held to the same standard
  as the services — but it is **not** inside the coverage target, because a coverage number for a
  runner that starts subprocesses measures the fixture rather than the service.
- The web page's Cancel button was verified by hand in Stage 12 and is not covered by a case here.
  Making it covered would need a second interaction channel in the runner (a cancel between two
  steps), and no case currently needs one; it is left as a known gap rather than implied as tested.
