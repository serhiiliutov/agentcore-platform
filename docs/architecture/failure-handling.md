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

### The budget has to bound each call, or it does not bound the turn

Checking the clock only *between* steps looks like a deadline and is not one. A tool that never
returns would hold the turn open forever, because the next checkpoint is never reached to notice
the budget was gone. The check would be correct at every moment it ran, and would never run.

So the loop grants each call the smaller of what the turn has left and the step's own ceiling, and
passes that down to the MCP client. Two details are load-bearing:

**The grant is computed before the call, not inside it.** A step that starts with nothing left is
refused rather than started and then abandoned, so the turn never begins work it has already
decided it cannot wait for.

**A step already in flight is still never torn down.** The grant bounds *how long* a call may take,
not whether it is interrupted — exactly the rule cancellation follows, for exactly the same reason.
A call that was granted a slice, started, and overran is allowed to finish and is recorded. The next
checkpoint is what stops the turn.

`McpToolExecutor.call` takes the smaller of the granted slice and its own configured ceiling, and
tests for `is None` rather than truthiness: `0.0` is a legitimate slice for a turn with nothing
left, and a truthiness check would silently replace it with the larger default — turning the one
case where the turn should stop into the case where it runs for the full timeout.

## An exhausted retry budget says how hard the client tried

The retry policy re-raises the last failure when it runs out of attempts. On its own that is a bug
in the message rather than the mechanism: the failure arrives carrying the sentence a *single*
attempt would have produced, and for a rate limit that sentence is "the ticketing API is rate
limiting this client. Waiting is required before retrying."

That is a true statement about the backend and a false one about the situation. The client has
already waited, backed off, and tried four times — telling a model to wait and retry is telling it
to do the one thing that has just been established not to work, and it invites a user to retry by
hand at the moment the system has decided retrying is pointless.

`TicketingError` carries `attempts`, set by `call_with_retry` on the way out, and exposes
`user_message`: the sentence a person should read, which knows how many attempts were made.
`_describe` checks it *before* any per-kind sentence, because an exhausted budget changes what the
right advice is rather than merely adding detail to it.

The two messages stay separate because they answer different questions. `message` describes what
the backend did — what a log wants, and what `str(error)` keeps. `user_message` answers "is trying
again worthwhile" — what a reader needs, and what a log has no use for. Collapsing them would make
one of the two audiences read the wrong thing.

### A retry only happens if the failure is raised where the loop can see it

The policy was correct and the retry never ran. `call_with_retry` wrapped the HTTP *request*, and
the status code was translated into an exception *after* the loop had returned. A 429 is a response,
not an exception, so `attempt()` returned it as a success, the loop counted a success, and the
translation happened once the loop was over. Nothing retried, ever.

Everything downstream of that was fine and pointless: the taxonomy classified 429 as retryable
correctly, `Retry-After` was parsed correctly, the backoff arithmetic was correct, and a unit test
of `call_with_retry` passed. The defect was in the *composition*, one layer above all of it.

So the translation sits inside the attempt. Two consequences that are easy to get wrong in the other
direction:

**A terminal failure still fails immediately.** `call_with_retry` checks `is_retryable` and
re-raises anything it cannot retry without consuming an attempt, so a 404 costs one request, not
four. Moving the translation inward does not make everything retryable; it makes the decision
reachable.

**The retry loop must not see a success it should not.** Returning the response from `attempt` and
inspecting it afterwards is the natural shape, and it is exactly the shape that silently disables
the policy. Any guard that inspects a *result* rather than raising an *error* has this hazard, and
the shape reads as correct right up until something counts the attempts.

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
- `tests/unit/test_error_taxonomy.py` — that an exhausted retry budget records its attempt count
  and reports it, that a single attempt says only what happened, and that a successful retry comes
  back clean rather than carrying a failure's history.
- `tests/contract/test_chaos.py` — that the mock's injected failures are *identifiable*: a flaky
  read never succeeds, and a failure says which kind it is. This is the layer the taxonomy above
  is derived from rather than a restatement of it — the mapping in `errors.py` is a reading of
  these responses, so a change here is a change to what the taxonomy should say.
- `tests/integration/test_failure_paths.py` — the same claims from *outside*, over a real socket,
  with a real server and a real ledger: that a failure storm writes nothing, that the budget is
  finite and reports how hard it tried, and that the same request key reused after the API recovers
  yields exactly one ticket.

This list is the reason that last file exists. Every test above it passed while the retry policy
described in this document was dead code, because each one tests a *part* and the defect was in how
the parts were joined. A unit test cannot see the seam between a retry loop and the translation that
feeds it; only a real request can.
