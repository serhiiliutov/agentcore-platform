# Stage 12 — failure hardening, deadlines, and cancellation

**Status: complete. Six defects found by running it; all fixed and verified end to end.**

This is the assignment's optional extra. It is worth doing because it is the stage where the
*unhappy* paths are made first-class rather than left to whatever the happy path happens to do
when a call is slow, a rate limit lands, or a human changes their mind mid-turn.

## What the plan asks for

- One global deadline budget, honoured across the loop and the MCP calls.
- `transient_exhausted` turned into an honest, user-facing message.
- Cooperative cancel tokens threaded through the loop, the MCP calls and the write guard.
- `POST /invocations/{request_id}/cancel`.
- A Cancel button in the page.
- `HealthyBusy` while background work runs.
- `docs/architecture/failure-handling.md`.

**Definition of done.** A cancelled run performs **no write** even when cancellation lands
mid-tool-call. A rate-limit storm across propose *and* approve still yields exactly one ticket. A
timeout run tells the user it gave up rather than retrying forever. Every one of these is
replayable from its trace alone — which is what Stage 10's spans were built for, and this is the
stage that finally tests that claim.

## Design decisions to carry into the implementation

These are decided now, before code, because two of them constrain the shape of everything else.

**The deadline is one budget, not a per-call timeout.** A per-call timeout cannot answer the
question a user asks: "why did this take 40 seconds". Three calls that each took 13s under a
per-call 15s ceiling is a turn that succeeded by accident and will fail as soon as it makes a
fourth call. The budget is decremented by the *elapsed wall time* of each step, and every step is
given `min(remaining, its own ceiling)` — so the last step cannot borrow time the turn does not
have. The loop stops with a named reason when the budget is exhausted, rather than running into a
per-call timeout and reporting that instead.

**Cancellation is cooperative and checked at step boundaries, never mid-write.** A cancel that
interrupted a tool call in flight would leave exactly the ambiguity Stage 4's ledger exists to
resolve — did the ticket get created? So the token is checked *before* a step starts, and a cancel
observed while a **write** is executing lets that write finish and then stops. Stopping between
propose and approve is free; stopping in the middle of executing an approved write is not, and the
difference is worth encoding rather than hoping.

**A cancelled turn is a state, not an exception.** It follows the pattern the write executor
already uses: the run ends with `stop_reason="cancelled"`, the partial transcript is kept, and the
row is not left mid-flight. A caller handed a `CancelledError` cannot distinguish "the user
changed their mind" from "the process is dying".

**`HealthyBusy` reports readiness, not liveness.** A service that answers `/ping` while a turn is
mid-flight is healthy and busy, and a load balancer or a supervisor should be told the difference —
otherwise a long turn looks like a stalled process and gets killed, which is the failure mode this
whole stage exists to avoid.

**Nothing here is allowed to make a write happen twice.** Every mechanism added in this stage is a
way to *stop* doing something. A cancel, a deadline, or an exhausted retry budget must all be
incapable of causing a second execution; if any of them can, it is wrong regardless of what else it
does. The exactly-once guarantee outranks every feature in this stage.

## Why this comes after Stage 11

Every failure path here has to be *reachable* to be tested, and until Stage 11 the service could
not serve a request at all: five defects stood between the assembled app and one working turn. A
deadline budget that is never reached, or a cancel endpoint nothing can call, would have passed
whatever unit tests were written for them while being unreachable in the running system.

The trace substrate this stage leans on is Stage 10's, and the idempotency guarantee it must not
break is Stage 4's. Both are in place and verified.

## Note on the Stage 11 deviation

The master plan for Stage 11 specifies a separate `services/web` on nginx (port 3000) proxying to
the agent. That was not built. The page is served by the agent process at `/`, and that decision is
recorded in `stage-11.md`: it is an ordinary client of the existing contract, so a second
container, a port and a CORS policy would have bought nothing. The assignment's requirement — a
thin page showing tool calls with approve/decline on a write — is met and was verified in a real
browser.

This matters here because the plan's Stage 12 deliverables mention wiring a Cancel button, which
assumes the nginx layout. The button belongs in `ui/page.py` alongside the approve/decline
controls, talking to `POST /invocations/{request_id}/cancel` on the agent directly.

## What was built

| Piece | Where | What it is |
| --- | --- | --- |
| Deadline and cancel token | `agent/budget.py` | One turn budget, one cooperative flag; no I/O, injected clock |
| Checkpoints | `agent/loop.py` | Both checks, between steps and never inside one |
| Stop-reason translation | `agent/handler.py` | The loop's vocabulary mapped to the contract's |
| Run registry | `agent/inflight.py` | Run id → token, in memory, scoped by user |
| Cancel endpoint | `api/cancel_routes.py` | `POST /invocations/{session_id}/cancel` |
| Cancel button | `ui/page.py` | Disabled once pressed; reports `stopping`, not `stopped` |
| Failure design | `docs/architecture/failure-handling.md` | Why each mechanism is shaped as it is |

## The six defects

Each was found by exercising the running system, not by reading it. The order is the order they
appeared, and the sixth is the one worth reading.

1. `steps` was counted before the checkpoint, so a turn cancelled on arrival reported `steps=1`
   having made no provider call — a number someone would later use to explain a slow turn.
2. The loop's `"timed_out"` fell through the stop-reason map to `"error"`, so a turn that gave up
   reached the wire claiming to have failed.
3. `timed_out` and `cancelled` would have been translated to two names for one state had the map
   not been keyed to exactly the reasons the loop emits.
4. A deadline checked only *between* steps is not a deadline: a tool that never returns holds the
   turn open forever, because the next checkpoint is never reached. Fixed by granting each call
   `min(remaining, step ceiling)` and passing it to the MCP client.
5. An exhausted retry budget re-raised the last failure carrying the sentence a *single* attempt
   would have produced — "the API is rate limiting this client, wait and retry" — after the client
   had already waited and retried four times.
6. **The retry policy was dead code.** `call_with_retry` wrapped the HTTP *request*; the
   status-code translation happened *after* the loop had returned. A 429 is a response, not an
   exception, so the loop received every throttle as a success and exited on the first one. The
   taxonomy, `is_retryable`, and the `Retry-After` handling were all written, wired, unit-tested
   against `call_with_retry` directly, and never once applied to the failure they exist for.

Defect 6 is the one this stage's structure earned. It is invisible to a unit test of the retry
policy, because the policy was correct — what was wrong was *where it was applied*. It is invisible
to a contract test of the mock, because the mock was correct too. It needs a real client, over a
real socket, against a mock that actually fails.

## Why `tests/integration/` exists now

The Stage 12 definition of done names `make test-integration -k failure` as its verification
command. Running it before writing anything showed it selecting **zero tests**: nothing in the
repository carried the `integration` marker, so the command passed, printed nothing, and verified
nothing — a definition of done that had never once been executed.

The suite it now selects starts the starter kit's mock under `uvicorn` on a free port and drives the
real `TicketingClient`, its real `RetryPolicy`, and a real `IdempotencyLedger` against it over HTTP.
Four tests, one per claim: a failure storm writes nothing; the budget is finite and reports how hard
it tried; a storm does not retry forever; and the same `client_request_id` reused after the API
recovers produces exactly one ticket. It was defect 6 that the third and fourth of those caught.

## Verification

- `make test` — 340 passed (336 unit and contract, 4 integration).
- `devtasks lint`, `devtasks fmt`, `devtasks typecheck` — clean, 92 source files.
- `make test-integration -k failure` — 4 selected, 4 passed; previously 0 selected.
- Browser: the page loads with no console output, a proposal renders its arguments and a local
  expiry, approving executes the write, and the run reports `Created ticket TCK-1028`.

## Carried forward

The integration suite covers the failure paths from the outside. It does not yet cover cancellation
end to end — the route is exercised in-process through the assembled app, and a browser walkthrough
of a genuinely in-flight cancel (rather than a one-millisecond turn) would need a deliberately slow
provider. That is the honest gap in this stage, and it is recorded rather than glossed.
