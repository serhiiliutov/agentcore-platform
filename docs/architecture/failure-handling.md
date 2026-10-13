# Failure handling

What this service does when something goes wrong, why each failure is shaped the way it is, and
which guarantee each mechanism protects. The short version: **every mechanism here exists to stop
doing something, and none of them is permitted to cause a second write.**

That rule outranks every feature in this document. A cancel, a deadline, or an exhausted retry
budget that could produce a duplicate ticket would be wrong regardless of anything else it got
right — and the reason it is stated first is that failure handling is exactly where duplicates
come from, because the naive recovery for "did that work?" is to try again.

## The failure taxonomy

Five things can go wrong with a turn, and they are kept distinct because they call for different
responses from a human and from the service.

| What happened | Where it is decided | What the caller sees | Should a user retry? |
| --- | --- | --- | --- |
| A tool refused the call | the backend, via the MCP server | `executed`/`failed` on the approval, with the backend's own sentence | Only after changing the request |
| The transport could not reach a tool | `AgentLoop._call` | The failure rendered into the transcript, named | Yes, later |
| The turn ran out of wall-clock time | `Deadline` | `stop_reason: deadline` | Yes, expected to be slower |
| Someone cancelled the turn | `CancelToken` | `stop_reason: cancelled` | Only if they want to |
| The model kept calling tools | `LoopLimits.max_steps` | `stop_reason: max_steps` | Probably not; the question was too broad |

The two that are easy to conflate and must not be are the last two. A cancel is a decision
somebody made and should be answered as such; a timeout is the service admitting it gave up. A
caller that could not tell them apart would either retry a deliberate stop or apologise for one.

## The deadline is a budget, not a per-call timeout

`agent/budget.py` holds one `Deadline` per turn. It tracks wall-clock time spent and grants each
step `min(remaining, its own ceiling)`, less a small margin.

A per-call ceiling cannot answer the question a user asks — "why did that take forty seconds" —
and it fails in a way that looks fine until it does not. Three calls that each take thirteen
seconds under a fifteen-second ceiling is a turn that succeeded by accident, and the fourth call
fails with a message about *that call* rather than about the turn. The margin exists so the step
times out just inside the turn's budget and the reason reported is about the turn.

The clock is injected rather than read directly, so a test can exhaust a budget instantly instead
of sleeping through it. A deadline test that sleeps is the slowest test in a suite and the first
one to be marked skip.

## Cancellation is cooperative, and never interrupts a write

`CancelToken` is a one-way flag somebody else sets. It is not a signal that tears down a
coroutine, and that is deliberate.

The loop checks it in `_checkpoint()`, called **between steps and never inside one**. So a step
already running is allowed to finish. This matters most for writes: a cancel that landed in the
middle of an executing write would re-open exactly the ambiguity the idempotency ledger exists to
close — did the call reach the backend? — and the honest answer would be that nobody knows.
Stopping between a proposal and its approval costs nothing. Stopping halfway through executing one
costs a ticket nobody can account for.

`POST /invocations/{session_id}/cancel` is how a person reaches the flag. It answers `stopping`,
not `stopped`, because that is what the mechanism delivers. It answers `404` when nothing is in
flight, and `501` when the registry is not wired — kept distinct so a deployment mistake never
looks like a finished turn.

The registry (`agent/inflight.py`) is deliberately **in memory**. A cancel has to reach a running
coroutine in *this* process; a database row would tell a different worker, or a restarted one, that
a run it does not have should stop, and the token it names would not exist there. It is therefore
lost on restart, and a run a restart killed cannot be cancelled because it is already over.

## A refused write is not a success

The single most important defect this stage fixed, and the one worth understanding before touching
the write path.

One tool result had two readers with opposite requirements. `McpToolExecutor.call` renders a
result for the loop, which needs a sentence it can show a model — failures included — so it returns
`as_model_text()`, which prefixes a failure with `ERROR: `. The write path read that same string,
and nothing in a `str` says whether it succeeded. So a refused write was recorded as `executed`,
and the reported detail began with `ERROR: ` — the writer was reading its own error marker as
prose.

The fix was not to parse the string more carefully. `WriteToolExecutor.call_checked` raises
`ToolCallFailedError` when the result reports an error, so only one of the two readers is handed a
value it could misread. The existing `except` branch turns that into a terminal state.

This matters more than it looks: an `approved` row is retried by every sweep. A single refusal
recorded as success was not one wrong row — it was a stream of duplicate attempts.

## A cancelled or expired turn is a state, not an exception

Both come back as a `TurnResult` with a `stop_reason`, carrying the partial transcript and the
tool-call records, following the pattern the write executor already uses.

A caller handed an exception cannot distinguish "the user changed their mind" from "the process is
dying", and those want opposite responses. A caller handed a state can render it.

The reported `steps` counts only steps that actually began: it is incremented *after* the
checkpoint. Counting an announced-but-abandoned step would put a number in the trace that somebody
would use to explain a slow turn, and it would be a lie.

## What is deliberately not attempted

**No automatic retry of a write.** Retrying is the reliable way to create a duplicate ticket, and
the ledger can only resolve an ambiguity it was told about. A write that failed is reported as
failed, and a person decides.

**No interruption of an in-flight tool call.** Described above; it is the one place where stopping
is more dangerous than continuing.

**No durable record of a cancel.** A cancel is a request to stop, not a state of the world. The
turn it stopped records its own outcome, which is the durable fact.

**No claim that a cancel prevented an effect.** The response says `stopping`. A design that said
`stopped` would be promising something cooperative cancellation cannot deliver.

## Where this is verified

- `tests/unit/test_budget.py` — the deadline arithmetic and the token, against an injected clock.
- `tests/unit/test_loop_control.py` — that cancellation and expiry are checked between steps and
  never inside one; that a step in flight completes; that the two stop reasons stay distinct; that
  every reason the loop can emit has a contract translation.
- `tests/unit/test_inflight.py` — that a cancel reaches the right run and no other, and that two
  users sharing a session id do not cross over.
- `tests/unit/test_cancel_route.py` — the endpoint, driven through the assembled app, because a
  route nothing can reach passes its own tests while being absent from the running system.
- `tests/unit/test_approval_gate.py` — that a *reported* failure is not recorded as executed.
