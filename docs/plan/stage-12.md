# Stage 12 — failure hardening, deadlines, and cancellation

**Status: in progress. Scope and decisions recorded; implementation follows.**

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
