# Stage 17 — reading the assignment back against the running system

**Status: complete.** Four gaps between what the assignment's contract says and what the code
actually did, all four fixed and verified through the real surface: **446 tests**, **22 of 22 eval
cases**, `ruff`, `mypy --strict` across 104 files, and the contract's own single-container smoke test
built and run with its documented body. This stage exists because the question asked was "is this
fully implemented before I send it", and the answer up to here had been assembled from the plan
rather than from the contract.

## What this stage is for

Stages 0–16 built to the plan and then re-verified from a fresh clone. That proves the *repository*
is coherent; it does not prove the *assignment* was met, because the plan is mine and the contract is
theirs. So this stage started from `CONTRACT.md` and `docs/agent-platform-assignment/` and asked, for
each sentence, which line of code makes it true — and, where the answer was "none", whether the
sentence was a requirement or a suggestion.

The audit was written as scratch (`project_info__1.md` at the repository root) and is **not** part of
this commit: it was a working artifact, and its findings are the list below, each with what settled
it. Nothing in this stage's record depends on that file existing.

Four things came back "none". One was a genuine harness defect rather than a gap: `search_files` is
unusable in this environment (`Could not find ripgrep binary`), so the audit was done with
`read_file`, `read_batch` and `list_files` only. That is why the findings below name files and lines
rather than pattern counts — and why the "no stale claim remains" check at the end is a `Select-String`
over a named file list rather than a repository-wide grep.

## Finding 1 — the contract's approval flow was not implemented (blocker)

`CONTRACT.md` is explicit, and this is the sentence the whole assignment hangs on:

> "The user's answer arrives as the next `/invocations` call in the same session
> (`{"prompt": "yes, go ahead"}`). Your service decides what counts as approval, what counts as a
> decline, and what to do if the user asks something unrelated instead."

No code read it that way. `AgentLoop.run` never consulted a session's pending approvals,
`InvocationHandlerFactory.__call__` ignored them, `providers/scripted.py` had no rule for "yes", and
`ui/page.py` always called the decide route. The *only* way to approve anything was
`POST /invocations/approvals/{id}/decide` — an endpoint `CONTRACT.md` does not describe and which
`AGENTCORE.md` already flags as outside the Runtime contract. So an evaluator following the
documented flow would have seen the write never happen, with the proposal sitting `pending` until it
expired.

Worse, the documentation asserted the opposite: `docs/architecture/approval-state-machine.md` said a
conversational "yes" *did* work. It did not. That is the failure mode this project's own README calls
the one to guard against, and it survived sixteen stages of green gates because every approval test
drove the button.

**What was built.** A message that is a decision now reaches the same gate the button uses, with one
implementation of "decide" and no second path to a write:

| Piece | What it does |
|---|---|
| `agent/decisions.py` | `classify(prompt) -> "approve" | "decline" | None` — a pure function of the text, closed vocabulary, first word decides the polarity, the whole message must agree with it |
| `agent/handler.py` | before the loop: if a decision is read and an applier is installed, apply it and return that as the turn's result; otherwise carry the still-pending proposal on the response |
| `factory.py::build_decision_callable` | the one place the gate is bound to a session; it also records an `execution` span, which is what closes finding 4 |
| `agent/handler.py::_wire_record` | one builder for a tool-call record, so `arguments`/`input` and `outcome`/`status` can never be filled in inconsistently |

**Three answers, not two.** "Unrelated" is a first-class outcome: the proposal stays exactly where it
was, the turn is answered normally, and the outstanding proposal is returned on the response — so a
client that renders `pending_approval` keeps showing the user what is waiting for them. A message
mixing polarities ("no, I mean yes") is treated as unrelated on purpose: the honest reading is
unclear, and the safe response to unclear is to ask rather than to write.

**Verified over the real contract** by three new eval cases — `approval_by_message` (6 checks),
`decline_by_message` (6) and `unrelated_message_keeps_the_proposal` (6) — and by
`tests/unit/test_message_approval.py`, which asserts each outcome against the same gate the button
uses, including the case where **no applier is installed at all**: the seam is optional, and its
absence means a message is just a message rather than a write nobody authorised.

And in a container, through the HTTP contract, because that is the surface the assignment describes:

```
4-propose:        "stop_reason": "awaiting_approval", "pending_approval": {"approval_id": "2a4c89eb-…"}
5-answer yes:     "stop_reason": "completed",
                  "tool_calls": [{"name": "create_ticket", "outcome": "executed",
                                  "summary": "Created ticket TCK-1025."}]
6-tickets now:    {"status":"ok","flaky":false,"tickets":25}      ← was 24 before the two calls
```

The same two calls were then run against the **composed** stack — `docker compose up -d --build`,
three services, MCP over streamable HTTP, a different transport from the image's stdio — because the
contract's flow has to hold on the topology the README documents and not only on the convenient one:

```
tickets before:   24
propose:          stop_reason=awaiting_approval  outcome=proposed  approval=62484dd4-785c-482c-941b-3f4d0e84880e
answer yes:       stop_reason=completed          outcome=executed  summary=Created ticket TCK-1025.
tickets after:    25
```

Two topologies, one implementation: the decision path is bound in the composition root, so it is the
same `ApprovalGate.decide` whether the agent reaches the MCP server as a subprocess or as a container
on a network.

## Finding 2 — the contract's smoke test could not run (blocker)

`CONTRACT.md` opens with:

```bash
docker build --platform linux/arm64 -t my-agent .
docker run -p 8080:8080 --env-file .env my-agent
```

There was no `Dockerfile` at the repository root. The first command a reviewer would try failed with
`open Dockerfile: no such file or directory`, before any of the work was visible. The starter kit's
own README only requires "one `docker compose up` starts everything", which the per-service
Dockerfiles satisfy — so this was not a gap against the kit. It was a gap against the contract this
service was written to.

The same section's example body was also rejected:

```json
{"prompt": "What is the status of TCK-1002?"}
```

No `user_id` → 422, asserted *deliberately* by `tests/contract/test_agent_contract.py`. A documented
smoke test that fails validation reads as a broken service before it reads as a careful one.

**What was built.** A root `Dockerfile` and `standalone-entrypoint.sh` that put the agent, the MCP
server (over stdio) and the ticketing mock into one container — the only composition a bare
`docker run -p 8080:8080` can reach. The three-service topology is unchanged and is still what
`docker compose up` gives you; this image is the contract's shape, and the Dockerfile says so in its
header rather than pretending it is the deployment.

The entrypoint repairs, and logs, the three values in `.env.example` that only make sense under
Compose, each of which was a real defect found while building it:

| Value | Why it cannot work in one container |
|---|---|
| `TICKETING_BASE_URL=http://ticketing:8000` | `ticketing` is a compose service name; nothing resolves it here. Checked by `getaddrinfo`, so a URL pointing anywhere reachable is left untouched |
| `MCP_TRANSPORT=http` | expects a second container on `MCP_HOST:MCP_PORT`. Probed with a socket; falls back to stdio, which is why the image ships the MCP server |
| `TICKETING_API_KEY=${API_KEY}` | **`docker run --env-file` does not interpolate** — compose does. The value arrives as the literal string `${API_KEY}`, so every tool call would have returned 401 while `/ping` stayed healthy |

`user_id` now defaults to one named, shared scope (`DEFAULT_USER_ID = "anonymous"`) instead of being
required. That keeps every user-scoped read — facts, traces, approvals — keyed on a real identity,
keeps `""` a 422 because a blank scope is a client bug rather than an omission, and is documented
where it is decided (`api/models.py`) and where a caller will meet it (README).

Verified end to end, in the container, with the contract's body and no user id:

```
1-ping:              {"status":"Healthy"}
2-invocation:        "tool_calls": [{"name": "get_ticket", "arguments": {"ticket_id": "TCK-1002"},
                                     "outcome": "executed", "duration_ms": 16,
                                     "summary": "Ticket TCK-1002: Cannot reset password via
                                                 self-service portal [in_progress] …"}]
```

`docker build` on this host is `amd64`; `--platform linux/arm64` cross-builds in CI (stage 14). An
ARM64 *run* remains unclaimed, as it has since stage 14, and still says so in the README.

## Finding 3 — response fields drifted from the contract's example (medium)

The contract's example response names `input`, `status` and `duration_ms`, and
`pending_approval.action`. The service published `arguments`, `outcome`, no duration, and
`tool_name`. A harness written against the contract's example would have read four empty fields and
reported success.

Both vocabularies are now published side by side in one object — the contract's is what it says it
will read, and `arguments`/`outcome` are what every module here, the page and the eval scorer speak.
`_wire_record` is the single place that builds a record, so the pair cannot be half-filled, and a
test asserts the pairing directly. `duration_ms` is now measured by the loop rather than estimated:
`None` for a proposal (nothing ran) and an integer for an executed call.

Renaming instead was rejected: the blast radius is every call site, the trace, the page and the
scorer, to no benefit the contract asks for — it fixes the *key* and explicitly leaves the vocabulary
to us.

## Finding 4 — traces recorded one span, and the documentation described five (medium)

`docs/architecture/observability.md` and `AGENTCORE.md` both described a set of spans — per model
call, per tool call with arguments and result, per retry, per approval decision, per provider call.
Production emitted exactly one: `MODEL_TURN`, named `invocation`, from `agent/handler.py`, with a
`prompt_chars` attribute and `total_tokens` always `0`. Nothing in `agent/loop.py`,
`agent/execution.py`, `agent/memory.py`, `mcp/client.py`, `api/cancel_routes.py`, `agent/inflight.py`
or `agent/budget.py` held a `Tracer` at all.

**What was built**, and the two things that are still deliberately not:

| Recorded | Where | Answers |
|---|---|---|
| `model_turn` — one per invocation | `agent/handler.py` | which provider, how many model turns, how long, whatever tokens it reported |
| `tool_call` — arguments, outcome, `is_error`, duration | `agent/loop.py` | what was called, with what, how it went, how long it took |
| `proposal` — `approval_id`, `arguments`, `state` | `agent/loop.py` | that the turn stopped for a human, and what would run |
| `execution` — `approval_id`, `outcome`, `duration_ms` | `factory.py` | what the decision did, **in its own trace**, because the decision is a separate request |
| `memory` — `recall_facts`, `remember_facts` | `agent/handler.py` | how many facts reached the prompt and which keys were learned — never the values |

Not recorded, and now stated as such in three places rather than implied: **one span per invocation
rather than per provider round trip**, and **no spans for retries**, which happen inside the MCP
server and return a single outcome. Both are honest limits of the design, so they are named instead
of dressed up.

The tokens column is now only ever zero when the provider reports nothing — which the deterministic
provider used by the evals genuinely does — and both documents say so.

## Defects this stage found in its own work

Recorded because they are the substance, and because each was caught by a gate rather than by
reading:

1. **The `SpanRecorder` protocol did not exist.** The loop's new `spans` parameter was typed as
   `Any`, which made every call site unchecked and made a `Tracer` and a test double
   indistinguishable to mypy. Fixed by defining the protocol and having `Tracer` satisfy it —
   `mypy` was green before *and* after, so this changed what "green" meant.
2. **`Tracer.__exit__` would have recorded blank spans for the whole decision path.**
3. **"don't" worked and "dont" did not.** Found by writing the vocabulary test rather than by
   reasoning: a refusal typed on a phone would have been read as unrelated, and unrelated leaves the
   proposal pending — to the user, being ignored.
4. **"please proceed" and "never mind" were both unrelated.** Fixed with a politeness rule
   (`please` is skipped as a lead word and judged by what follows it, while `please` alone stays
   unrelated) and one vocabulary word.
5. **`.dockerignore` could not re-include the mock.** Excluding the directory outright made the
   `!` exception lines silently do nothing, and the failure surfaced much later as
   `COPY failed: file not found`. Fixed with `mock-ticketing/*`, and the comment now says why.
6. **Three documentation claims were false** — the tracing set (finding 4), the conversational
   approval (finding 1), and `AGENT_APPROVAL_TTL_MINUTES`, a variable that has been
   `AGENT_APPROVAL_TTL_S` for several stages. All three corrected at the source rather than
   annotated.
7. **The eval harness's `tickets_delta_before_decision` requires a `decide` step** to have set its
   baseline. The three new cases therefore assert `tool_not_executed` at turn 0 plus `tickets_delta`
   instead — the same fact, expressed in the vocabulary that fits a message-driven case. It is
   recorded here because the next person to write a case will hit it.

## Definition of done

| Criterion | How it was checked | Result |
|---|---|---|
| The contract's conversational approval works | two calls in one session in the single-container image: propose, then `{"prompt": "yes, go ahead"}` | **met** — `stop_reason: completed`, `outcome: executed`, mock tickets 24 → 25 |
| The same flow works on the composed topology | the same two calls against `docker compose up -d --build` (three services, HTTP MCP), plus `test-integration` | **met** — `awaiting_approval`/`proposed` then `completed`/`executed` (`Created ticket TCK-1025.`), tickets 24 → 25; integration tests **4 passed, 446 deselected** |
| A decline writes nothing; an unrelated message keeps the proposal | `decline_by_message`, `unrelated_message_keeps_the_proposal` | **met** — both 6/6, `tickets_delta` 0 |
| Both decision paths share one gate | `tests/unit/test_message_approval.py` asserts the row transitions and the transport payload for the message path | **met** — 7 tests, spy transport called once with the stored arguments |
| The contract's smoke test builds and runs | `docker build -t agentcore-standalone .` then `docker run -p 8080:8080 …`, then `/ping` and the contract's own body | **met** — build exit 0; `Healthy`; `get_ticket` executed; MCP connected with 4 tools |
| The contract's example body is accepted | the invocation above, with no `user_id` | **met** — 200, and `test_invocations_defaults_an_absent_user_id` pins it hermetically |
| The response carries the contract's field names | `test_the_wire_record_carries_both_vocabularies`, plus the container's response | **met** — `input`/`status`/`duration_ms` all present and populated |
| Every span kind the docs describe is emitted | `tests/unit/test_loop_control.py` (tool call, failure, proposal) and the decision route's execution span | **met** — and the two that are *not* emitted are now stated in three documents |
| Nothing claimed that no command printed | the numbers above are copied from the runs in this file; the container output is quoted | **met** |
| No stale claim remains anywhere a reader would look | `Select-String` over README, AGENTCORE, `docs/architecture`, `docs/runbooks`, `docs/media`, `services/agent/README.md` for the old counts and the old tracing wording | **met** — plan history is deliberately *not* rewritten; stage records state what was true then |
| Gates | `ruff`, `ruff format --check`, `mypy --strict`, `pytest`, `detect-secrets` | **met** — lint and format clean, `Success: no issues found in 104 source files`, 446 passed / 4 deselected, secrets "nothing new" |
| The suite as a whole | `python -m devtasks eval` | **met** — **22 of 22 passed in 46.8s** |

## What this stage does not claim

- **The ARM64 run**, unchanged since stage 14: the image cross-builds, the run is not claimed, and
  the README still says so. The single-container image was built and run `amd64` locally.
- **The single-container image is not the topology.** `docker compose up` remains the supported way
  to run this, one service per container. The root `Dockerfile` exists because the contract's smoke
  test is written as a single container, and it is documented as a convenience rather than
  presented as the deployment.
- **Concurrency is still untested.** The message path reads a proposal, then decides it, in one
  process. A second process deciding the same proposal at the same moment is refused by the
  repository's claim-before-execute, but that race is not asserted — it is the same gap listed in
  README section j and in `AGENTCORE.md`'s verification table.
- **The classifier is a vocabulary, not an understanding.** It reads a closed set of phrases and
  treats everything else as unrelated, which means a user who phrases refusal in an unexpected way
  gets one more sentence asking them to confirm. That asymmetry is deliberate and the module
  docstring defends it; it is not a general solution to intent recognition.
