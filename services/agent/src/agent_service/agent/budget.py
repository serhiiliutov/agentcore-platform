"""One deadline for a whole turn, and cooperative cancellation.

Two small pieces of state, kept in their own module because both answer questions the loop, the
MCP client and the write guard all have to answer the same way, and a shared answer is only
possible if there is one object to share.

**A deadline is a budget, not a per-call timeout.** A per-call ceiling cannot answer the question a
user actually asks — "why did that take forty seconds" — and it fails in a way that looks fine
until it does not: three calls that each take thirteen seconds under a fifteen-second ceiling is a
turn that succeeded by accident, and the fourth call fails with a message about *that* call rather
than about the turn. So the deadline tracks wall-clock time spent, and each step is granted
``min(remaining, its own ceiling)``. The last step therefore cannot borrow time the turn does not
have, and the loop can say "the turn ran out of time" rather than "this call timed out".

**Cancellation is cooperative, and never interrupts a write.** The token is a flag somebody sets,
not a signal that tears down a coroutine. That is deliberate: a cancel that landed in the middle of
an executing write would re-open exactly the ambiguity the idempotency ledger exists to close — the
call may or may not have reached the backend. So the loop checks the token *between* steps, and a
write already in flight is allowed to finish and be recorded before the run stops. Stopping between
a proposal and its approval costs nothing; stopping halfway through executing one costs a ticket
nobody can account for.

Neither type does any I/O, reads a clock beyond the one injected, or knows what a tool is. They are
pure state so that a test can exhaust a budget or trip a cancel without waiting or sleeping.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

#: Padding subtracted from a granted slice so the caller's own timeout fires just before the turn's
#: budget runs out, and the reason reported is about the turn rather than about the last call.
SLICE_MARGIN_S = 0.05


class DeadlineExceededError(RuntimeError):
    """The turn's time budget is spent.

    Raised rather than returned so a caller cannot continue by accident, and carrying the budget
    that was exhausted so the message can say how long the turn was allowed, which is the number an
    operator needs to change.
    """

    def __init__(self, budget_s: float, spent_s: float) -> None:
        super().__init__(
            f"the turn exceeded its {budget_s:g}s budget after {spent_s:.2f}s; "
            f"raise AGENT_TURN_BUDGET_S if this is a normal workload"
        )
        self.budget_s = budget_s
        self.spent_s = spent_s


@dataclass
class Deadline:
    """How much wall-clock time one turn has left.

    ``now`` is injected rather than read directly, so a test can exhaust a budget instantly instead
    of sleeping through it — the alternative is a suite that takes as long as the timeouts it is
    testing, which is how deadline tests end up skipped.
    """

    budget_s: float
    #: The slice granted to a step that does not ask for less. Kept here so the loop and the MCP
    #: client cannot disagree about it.
    step_ceiling_s: float = 20.0
    _started: float = field(default=0.0, init=False, repr=False)
    _now: Callable[[], float] = field(default=time.monotonic, repr=False)

    def __post_init__(self) -> None:
        if self.budget_s <= 0:
            raise ValueError(f"a turn budget must be positive, got {self.budget_s!r}")
        self._started = self._now()

    @property
    def spent_s(self) -> float:
        """Wall-clock seconds used so far."""
        return max(0.0, self._now() - self._started)

    @property
    def remaining_s(self) -> float:
        """Seconds left, never negative.

        Clamped at zero rather than allowed to go negative: every caller compares it to something,
        and a negative remaining time silently satisfies a check like ``remaining < window`` in a
        way that reads like there is time left.
        """
        return max(0.0, self.budget_s - self.spent_s)

    @property
    def expired(self) -> bool:
        """Whether the turn is out of time."""
        return self.remaining_s <= 0.0

    def grant(self, *, ceiling_s: float | None = None) -> float:
        """The timeout to give the next step.

        The smaller of what is left and the step's own ceiling, less a small margin — so a step
        times out *just* inside the turn's budget and the failure is attributed to the turn, which
        is the honest description, instead of to whichever call happened to be running.

        Raises `DeadlineExceededError` when nothing is left, rather than returning zero: a zero
        timeout
        handed to a client is a request that fails in the client's own vocabulary, and the caller
        would have to translate it back into "the turn is over" at every call site.
        """
        remaining = self.remaining_s
        if remaining <= 0.0:
            raise DeadlineExceededError(self.budget_s, self.spent_s)
        wanted = self.step_ceiling_s if ceiling_s is None else ceiling_s
        if wanted <= 0:
            raise ValueError(f"a step ceiling must be positive, got {wanted!r}")
        return max(0.001, min(remaining, wanted) - SLICE_MARGIN_S)


class RunCancelledError(RuntimeError):
    """The run was cancelled by someone with the authority to stop it.

    A named exception rather than returning a flag from every step, for the same reason
    `ApprovalTransitionError` is an exception: a caller that forgets to check a return value
    continues, and continuing is the one thing cancellation must not do.
    """


@dataclass
class CancelToken:
    """A one-way flag a run checks between steps.

    One-way and sticky: un-cancelling is not supported, because a run that resumed after a cancel
    would be a run nobody asked for, and the request that cancelled it has already been answered.

    Thread-safe by construction rather than by a lock. A bool assignment is atomic under CPython, a
    token is only ever set from false to true, and every read is of a value that is either "not yet"
    or "yes" — so a lock would serialise the loop to protect something that cannot be observed
    half-set.
    """

    _cancelled: bool = field(default=False, init=False, repr=False)
    #: Why it was cancelled, for the trace and for the message a user is shown.
    reason: str = ""

    def cancel(self, reason: str = "") -> None:
        """Mark the run cancelled. Idempotent, and never un-does itself.

        The first reason wins. A second cancel — a user clicking twice, a shutdown arriving after a
        request — must not overwrite the explanation of what actually happened.
        """
        if not self._cancelled:
            self._cancelled = True
            self.reason = reason

    @property
    def cancelled(self) -> bool:
        """Whether the run has been cancelled."""
        return self._cancelled

    def raise_if_cancelled(self) -> None:
        """Stop the run if it has been cancelled.

        The whole point of calling this *between* steps: it is a checkpoint, not an interrupt. A
        step already running is never torn down by it.
        """
        if self._cancelled:
            raise RunCancelledError(self.reason or "the run was cancelled")
