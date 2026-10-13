"""The turn deadline and the cancel token.

Both are pure state with an injected clock, which is what makes these tests fast: a deadline test
that slept through its own timeouts would be the slowest suite in the repository, and the first one
to be marked skip.

Every test here is about a specific way the budget or the token could be wrong — a negative
remaining time that reads as "time left", a cancel that could be un-set, a slice that lets the last
step borrow time the turn does not have.
"""

from __future__ import annotations

import pytest

from agent_service.agent.budget import (
    SLICE_MARGIN_S,
    CancelToken,
    Deadline,
    DeadlineExceededError,
    RunCancelledError,
)


class FakeClock:
    """A clock the test moves by hand.

    Injected rather than patched so the deadline's arithmetic is exercised against exact numbers
    instead of against whatever a real monotonic clock happened to do while the test ran.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _deadline(
    clock: FakeClock, *, budget_s: float = 30.0, step_ceiling_s: float = 20.0
) -> Deadline:
    return Deadline(budget_s=budget_s, step_ceiling_s=step_ceiling_s, _now=clock)


# --- the budget ---------------------------------------------------------------


def test_a_fresh_budget_has_all_of_its_time_left() -> None:
    clock = FakeClock()
    deadline = _deadline(clock, budget_s=30.0)

    assert deadline.remaining_s == 30.0
    assert deadline.spent_s == 0.0
    assert not deadline.expired


def test_time_spent_is_measured_from_construction() -> None:
    """The clock starts when the turn does, not when the first step does."""
    clock = FakeClock()
    deadline = _deadline(clock, budget_s=30.0)

    clock.advance(11.5)

    assert deadline.spent_s == 11.5
    assert deadline.remaining_s == pytest.approx(18.5)


def test_remaining_time_never_goes_negative() -> None:
    """The clamp is the point: ``remaining < other`` reads as "time left" when it is negative."""
    clock = FakeClock()
    deadline = _deadline(clock, budget_s=10.0)

    clock.advance(99.0)

    assert deadline.remaining_s == 0.0
    assert deadline.expired


def test_a_step_gets_the_smaller_of_its_ceiling_and_what_is_left() -> None:
    """The last step cannot borrow time the turn does not have."""
    clock = FakeClock()
    deadline = _deadline(clock, budget_s=30.0, step_ceiling_s=20.0)

    assert deadline.grant() == pytest.approx(20.0 - SLICE_MARGIN_S)

    clock.advance(25.0)
    assert deadline.grant() == pytest.approx(5.0 - SLICE_MARGIN_S)


def test_the_granted_slice_stops_short_of_the_budget() -> None:
    """So the step fails *inside* the turn's budget and the reason is about the turn.

    Without the margin the two deadlines race, and the message a user sees names whichever lost
    rather than the turn that ran out.
    """
    clock = FakeClock()
    deadline = _deadline(clock, budget_s=30.0, step_ceiling_s=20.0)

    granted = deadline.grant()

    assert granted < 20.0
    assert granted < deadline.remaining_s


def test_granting_from_an_exhausted_budget_raises() -> None:
    """A zero timeout would fail in the client's vocabulary, and every caller would translate it."""
    clock = FakeClock()
    deadline = _deadline(clock, budget_s=5.0)
    clock.advance(5.0)

    with pytest.raises(DeadlineExceededError) as caught:
        deadline.grant()

    assert caught.value.budget_s == 5.0
    assert "AGENT_TURN_BUDGET_S" in str(caught.value)


def test_a_budget_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        Deadline(budget_s=0.0)


def test_a_step_ceiling_must_be_positive() -> None:
    """A non-positive ceiling would grant no time and look like an exhausted budget."""
    clock = FakeClock()
    deadline = _deadline(clock)

    with pytest.raises(ValueError, match="step ceiling must be positive"):
        deadline.grant(ceiling_s=0.0)


def test_a_per_call_ceiling_can_be_narrower_than_the_step_ceiling() -> None:
    """The MCP client asks for less than the loop's default, and gets it."""
    clock = FakeClock()
    deadline = _deadline(clock, budget_s=30.0, step_ceiling_s=20.0)

    assert deadline.grant(ceiling_s=3.0) == pytest.approx(3.0 - SLICE_MARGIN_S)


# --- cancellation -------------------------------------------------------------


def test_a_fresh_token_is_not_cancelled() -> None:
    token = CancelToken()

    assert not token.cancelled
    token.raise_if_cancelled()


def test_cancelling_is_visible_and_stops_the_run() -> None:
    token = CancelToken()

    token.cancel("the user clicked cancel")

    assert token.cancelled
    with pytest.raises(RunCancelledError, match="clicked cancel"):
        token.raise_if_cancelled()


def test_cancellation_is_sticky() -> None:
    """A run that resumed after a cancel would be a run nobody asked for."""
    token = CancelToken()
    token.cancel("first")

    assert token.cancelled, "there is no un-cancel, by design"


def test_the_first_reason_wins() -> None:
    """A second cancel must not overwrite the explanation of what actually happened."""
    token = CancelToken()

    token.cancel("the user clicked cancel")
    token.cancel("the server is shutting down")

    assert token.reason == "the user clicked cancel"


def test_cancelling_without_a_reason_still_reads_sensibly() -> None:
    token = CancelToken()

    token.cancel()

    with pytest.raises(RunCancelledError, match="cancelled"):
        token.raise_if_cancelled()
