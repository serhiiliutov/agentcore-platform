# Runbook: the eval suite

How to run it, how to add a case, and how to read a failure. For what the suite is *for* — the
families, the latest results, and what it does not catch — see section e of the
[README](../../README.md).

## Running it

```bash
make eval                          # all 19 cases, table to stdout, results written to eval/results/
uv run python -m eval              # the same thing without make
uv run python -m eval --only memory # cases whose id contains "memory"
uv run python -m eval --quiet       # the table only, no per-case line as they run
```

Exit code is **0 only when every case passed** — that is what lets it gate a change. Two files are
written every run, and both are committed:

| File | Audience |
|---|---|
| [`eval/results/latest.md`](../../eval/results/latest.md) | a person: the table, the failure detail, and the assertions each case consumed |
| [`eval/results/latest.json`](../../eval/results/latest.json) | a tool, and a debugging aid: every response payload and the ticket counts around each case |

`--cases` and `--results` point at other files, which is how the deliberately-broken fixture below
is scored without touching the committed results.

## What it starts, and what it deliberately does not

**The suite does not use `docker compose` and does not need a running stack.** `eval/stack.py`
starts the mock and the agent as real servers on free ports, in-process, against a throwaway SQLite
file it migrates with `alembic upgrade head`. Four consequences:

- **It cannot pass by testing the assembly twice.** The runner would not catch a wrong container
  port or a missing healthcheck; those are what `make up-ci` and the screenshot task are for. What
  it *does* catch is a handler that was never wired, a route prefix that drifted, or a provider
  whose credentials were never read — the defects that survive a green unit suite.
- **No credentials and no network.** The provider is left at its default, `scripted`.
- **The schema comes from the migration, not the models.** `build_app` deliberately never calls
  `create_all` (ADR-0004), so a suite that passes is a suite passing against a schema a migration
  actually produces.
- **The ledger is per-run.** `TICKETING_LEDGER_PATH` points into the run's own directory. Without
  that, a ledger remembering a key from a previous run changes the *second* run's behaviour —
  a write is answered from the record, no ticket is created, and a case asserting one ticket fails
  for a reason that has nothing to do with the agent.

### One stack, and why

Cases run in order against **one** shared stack, so state accumulates between them. That is the
point: the approval cases write real tickets and the memory cases accumulate real facts, and a case
that only passed because it started from nothing would look correct here and fail in a real
conversation. Chaos is toggled per case on the mock rather than by restarting it — the middleware
reads the flag per request, so switching it is a real change of backend behaviour.

The exception is a case declaring a `stack` block. A turn budget and an approval TTL are read once,
when the application is built, so no per-request trick can vary them; such a case gets a stack of
its own, started and stopped around it. Three cases do, and two of them exist to prove that
*durable* state survives configuration being rebuilt underneath it — which the shared stack cannot
show.

A case may override exactly these, named as environment variables:

```
AGENT_REQUEST_DEADLINE_S   AGENT_APPROVAL_TTL_S   AGENT_MAX_STEPS
```

Values from an earlier case are cleared before the next stack, so the file's case order cannot
change whether a case passes. A developer's own value for any of the three is deliberately ignored
by the suite — a rule stated here rather than discovered as a surprise.

## What a case is

`eval/cases.json` is the whole suite. One case:

```json
{
  "id": "approval_respected",
  "family": "approval step respected",
  "why": "the write is proposed, not executed, and only the approval writes",
  "user_id": "eval-approve",
  "steps": [
    { "invoke": "please raise a ticket for the broken printer, high priority" },
    { "decide": { "approval": "previous", "approved": true } }
  ],
  "expect": [
    { "assertion": "stop_reason_is", "value": "awaiting_approval", "turn": 0 },
    { "assertion": "approval_pending", "turn": 0 },
    { "assertion": "tool_not_executed", "tool": "create_ticket", "turn": 0 },
    { "assertion": "tickets_delta_before_decision", "value": 0 },
    { "assertion": "decision_outcome_is", "value": "executed" },
    { "assertion": "tickets_delta", "value": 1 }
  ]
}
```

Six keys, and each is checked before the stack starts so a malformed case cannot be discovered
half-way through a run:

| Key | Meaning |
|---|---|
| `id` | unique; ids are how results are compared between runs |
| `family` | the group the report tables it under |
| `why` | what the case proves, in one sentence, quoted in the report |
| `user_id` | required — memory and every read are scoped by one |
| `steps` | the script, below |
| `expect` | the assertions |
| `chaos` | optional: switch the mock's failures on for this case |
| `stack` | optional: the environment overrides above, for a stack of its own |
| `second_user_id` + `second_user_prompt` | optional pair: another user states a fact, so an isolation case can prove it does not leak |

### The four step kinds

| Step | Effect |
|---|---|
| `{"invoke": "…"}` | one turn through `POST /invocations`; the session is carried to the next step, so multi-turn cases are a conversation |
| `{"decide": {"approval": "previous", "approved": true}}` | one decision through `POST /invocations/approvals/{id}/decide`; `previous` means the proposal the last invoke produced |
| `{"restart": true}` | tear the agent down and build it again, against the same database and the same mock |
| `{"sleep_s": 2}` | let time pass, so an expiry can happen |

`restart` and `sleep_s` are **not** recorded as steps, which is deliberate: recording them would
shift every `turn` index in a case, so a case restarting between a proposal and a decision would
have to count the restart when naming the turn it means — a trap for whoever writes the next one.

### What `restart` does and does not reproduce

It rebuilds the ASGI application and its settings, the database engine (disposing the old one — two
engines on one SQLite file is how a restart test produces a spurious "database is locked"), and the
lifespan, which stops and restarts the MCP client and its ticketing subprocess. What it does not
reproduce is a new process id. That is the honest limit: an approval that survives this survived a
rebuilt application reading a durable file. The same store under a genuinely new process is covered
by the repository tests, which need no server at all.

### The assertion vocabulary

Thirteen assertions, and the report prints which ones each case consumed — so a case cannot claim
more than it checks:

| Assertion | Passes when |
|---|---|
| `stop_reason_is` | the turn's `stop_reason` equals the value (`completed`, `awaiting_approval`, `failed`, …) |
| `tool_called` | the named tool was executed in that turn |
| `tool_not_called` | the named tool was **not** called in that turn |
| `tool_argument_is` | the named tool was called with an argument of that value — which is how "which page did you ask for" is asserted |
| `tool_not_executed` | the tool appears in a proposal and never as an executed call |
| `tool_error` | a tool call came back as an error, optionally of a named kind |
| `no_tool_calls` | the turn called nothing at all |
| `output_contains` | the reply mentions the text |
| `payloads_avoid` | no recorded payload contains the text — the assertion behind both the secret case and the isolation case |
| `approval_pending` | the turn ended with a pending proposal |
| `decision_outcome_is` | the decision's outcome (`executed`, `declined`, `failed`, `refused`) |
| `tickets_delta` | the mock's ticket count moved by exactly this much across the case |
| `tickets_delta_before_decision` | the same, but measured at the moment before the decision — the assertion that states the approval requirement exactly: proposing wrote nothing |

An unknown assertion is an **error, not a pass**: a case the scorer cannot interpret is a broken
case, and the report says so rather than scoring it as fine.

## Adding a case

1. Add the object to `eval/cases.json`. Nothing else registers a case.
2. Give it a unique `id` and a `family`, and write `why` as the sentence you would want in the
   report.
3. Assert with the vocabulary above. If the case needs a **new** kind of observation, that is a
   change to `eval/scoring.py` and a new row in the table — not a looser assertion.
4. Prove it can fail before you trust it (below).
5. `make eval` and commit `eval/results/latest.md` and `latest.json` with the change.

## Proving the suite can still fail

A suite that cannot fail is worse than no suite, because it gets cited as evidence. Two ways, both
run as part of the normal gates:

```bash
# 1. The committed broken fixture: cases whose assertions cannot pass. A unit test asserts it is
#    scored as failing, so the fixture cannot rot into passing.
uv run python -m eval --cases eval/fixtures/deliberately-broken.json --results /tmp/eval-broken
#    → exit code 1, and every case in it reported as failed

# 2. Break something real and watch the right case notice.
#    Flip one assertion (e.g. tickets_delta 1 → 2), run `make eval -- --only approval`, and
#    confirm it fails *before* reverting.
```

The second is the one that matters when adding a case: a new assertion that passes on the first run
has not yet demonstrated it can detect anything.

## Reading a failure

The per-case `FAIL` line names the case; `--quiet` off prints each failed assertion's detail. Then
look at [`latest.json`](../../eval/results/latest.json): it holds the **raw payloads** for every
step of the case and the ticket counts before, before the decision, and after. Almost every failure
is diagnosable from that file alone:

- `stop_reason` says whether the turn completed, waited for approval or failed;
- a `tool_call` step shows what was actually called and with which arguments;
- the ticket counts say whether a write reached the mock, which distinguishes "the agent did not
  try" from "the agent tried and the backend refused".

If the failure looks like a fault rather than a judgement, the same run's traces (`TRACE_DIR` in the
run's throwaway directory) and the guidance in
[`../architecture/observability.md`](../architecture/observability.md) apply.

## Two failure modes to expect while developing

- **A case that passes alone and fails in the suite.** Almost always shared state: the stack is one
  process by design, so a case that leaves a proposal undecided or a fact unexpired affects what
  follows. Fix the case, not the isolation.
- **A second run behaving differently from the first.** If it is about a write, check
  `TICKETING_LEDGER_PATH`. A ledger that outlives the mock is the one thing that makes the suite
  non-reproducible, and `eval/stack.py` sets it per run precisely because that happened.
