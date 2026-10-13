"""What the loop does when a turn is cancelled or runs out of time.

These tests exist because both mechanisms fail *quietly* when they are wrong. A missing cancel
check is a run that will not stop, and a missing deadline check is a turn with no ceiling;
neither shows up as a failing test unless something asserts the stopping behaviour directly.

The claim worth stating plainly, because every test below is a version of it: **a checkpoint is
between steps, never inside one.** Cancelling or expiring never tears down a step that is already
running, so a read that has begun completes and is recorded, and a write that is executing is
allowed to finish rather than being abandoned with its outcome unknown.

The stubs here are deliberately minimal — a provider that returns whatever it is told to, an
executor that echoes, and an approvals repository that is never reached. The loop's control flow
is the subject, so everything else is held as small as it can be while still being the real type.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import pytest

from agent_service.agent.budget import CancelToken, Deadline
from agent_service.agent.handler import STOP_REASON_MAP, to_invocation_response
from agent_service.agent.loop import AgentLoop, LoopLimits, TurnResult
from agent_service.db.repositories.approvals import ApprovalRepository
from agent_service.providers.base import (
    AssistantTurn,
    Message,
    ToolCallRequest,
    ToolSchema,
)
from agent_service.tools.registry import RegisteredTool, ToolRegistry

#: The clock the tests move by hand, so no test waits for a timeout it is testing.
START = 1000.0


class FakeClock:
    """A monotonic clock the test advances explicitly."""

    def __init__(self, start: float = START) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeProvider:
    """Returns scripted replies, and can act on the turn's control state as it does.

    The hook exists so a test can cancel the run or burn the clock *during* a step — which is
    exactly the situation the loop must handle at the next checkpoint rather than mid-step.
    """

    def __init__(self, turns: Sequence[AssistantTurn]) -> None:
        self._turns = list(turns)
        self.calls = 0
        #: Run after each reply is produced, before the loop acts on it.
        self.on_chat: Any = None

    @property
    def name(self) -> Any:
        return "scripted"

    async def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
        *,
        budget: int | None = None,
    ) -> AssistantTurn:
        self.calls += 1
        turn = self._turns.pop(0) if self._turns else AssistantTurn(text="done")
        if self.on_chat is not None:
            self.on_chat()
        return turn


class FakeExecutor:
    """Echoes each call, and counts the ones that happened."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append(name)
        return f"{name} ran"


class UnusedApprovals:
    """Stands in for the repository in runs that propose nothing.

    Every test here either cancels or expires *before* a write can be proposed, so this is never
    called. It is `cast` to the real type rather than being a real repository because standing up
    a database to prove that a cancelled run does not reach the database would be circular.
    """

    def propose(self, **_: Any) -> Any:
        raise AssertionError("a turn under test proposed a write; this test did not expect one")


def _registry(*, writes: Sequence[str] = ()) -> ToolRegistry:
    """A registry holding one read tool, plus any named writes."""
    tools = [
        RegisteredTool(
            name="search_tickets",
            description="Search tickets",
            input_schema={"type": "object"},
            requires_approval=False,
        )
    ]
    tools.extend(
        RegisteredTool(
            name=name,
            description=f"Write {name}",
            input_schema={"type": "object"},
            requires_approval=True,
        )
        for name in writes
    )
    return ToolRegistry(tools)


def _loop(
    provider: FakeProvider,
    *,
    executor: FakeExecutor | None = None,
    deadline: Deadline | None = None,
    cancel: CancelToken | None = None,
    max_steps: int = 8,
) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        tools=_registry(),
        executor=executor or FakeExecutor(),
        approvals=cast(ApprovalRepository, UnusedApprovals()),
        session_id="sess-1",
        user_id="alice",
        limits=LoopLimits(max_steps=max_steps),
        deadline=deadline,
        cancel=cancel,
    )


def _read_call(name: str = "search_tickets") -> AssistantTurn:
    """A reply that asks for one read tool."""
    return AssistantTurn(
        text="let me look",
        tool_calls=(ToolCallRequest(id="call-1", name=name, arguments={"q": "printer"}),),
        stop_reason="tool_use",
    )


# --- cancellation -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cancelled_run_never_reaches_the_provider() -> None:
    """The check is the first thing a step does, so a cancelled turn costs nothing."""
    provider = FakeProvider([AssistantTurn(text="should not be produced")])
    token = CancelToken()
    token.cancel("the user clicked cancel")

    result = await _loop(provider, cancel=token).run("raise a ticket")

    assert result.stop_reason == "cancelled"
    assert provider.calls == 0, "a cancelled run must not spend a model call"
    assert result.steps == 0


@pytest.mark.asyncio
async def test_cancelling_between_steps_keeps_the_calls_that_already_ran() -> None:
    """The partial work is reported rather than discarded.

    Cancelling after a read has already happened must not erase the fact that it happened: the
    record is what the trace and the response are built from, and a turn that hid the call it
    made before stopping would be reporting something that is not what occurred.
    """
    provider = FakeProvider([_read_call(), AssistantTurn(text="never asked")])
    executor = FakeExecutor()
    token = CancelToken()
    # Cancelled as the *first* reply is produced, so the read still runs and the second
    # checkpoint is what stops the turn.
    provider.on_chat = lambda: token.cancel("changed my mind")

    result = await _loop(provider, executor=executor, cancel=token).run("find the printer ticket")

    assert result.stop_reason == "cancelled"
    assert executor.calls == ["search_tickets"], "the read that began was allowed to finish"
    assert [record.outcome for record in result.tool_calls] == ["executed"]
    assert result.steps == 1
    assert "cancelled" in result.text


@pytest.mark.asyncio
async def test_a_normal_run_is_untouched_by_an_uncancelled_token() -> None:
    """The token is inert until somebody sets it."""
    provider = FakeProvider([AssistantTurn(text="here is your answer")])

    result = await _loop(provider, cancel=CancelToken()).run("what is open?")

    assert result.stop_reason == "end_turn"
    assert result.text == "here is your answer"


# --- the deadline -------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_exhausted_deadline_stops_the_turn_before_the_next_step() -> None:
    """Reported as having given up, not as having broken."""
    clock = FakeClock()
    deadline = Deadline(budget_s=5.0, _now=clock)
    provider = FakeProvider([AssistantTurn(text="should not be produced")])
    clock.advance(5.0)

    result = await _loop(provider, deadline=deadline).run("anything")

    assert result.stop_reason == "timed_out"
    assert provider.calls == 0, "an expired budget must not spend a model call"
    assert "gave up" in result.text


@pytest.mark.asyncio
async def test_the_deadline_is_not_checked_inside_a_step() -> None:
    """A step already running is never torn down, so the read completes.

    This is the distinction the whole design rests on. Burning the entire budget *during* a step
    must not abandon it: the next checkpoint stops the turn, and the call that was in flight is
    recorded as the thing that happened.
    """
    clock = FakeClock()
    deadline = Deadline(budget_s=5.0, _now=clock)
    provider = FakeProvider([_read_call()])
    executor = FakeExecutor()
    provider.on_chat = lambda: clock.advance(99.0)

    result = await _loop(provider, executor=executor, deadline=deadline).run("find it")

    assert executor.calls == ["search_tickets"], "the step in flight was allowed to complete"
    assert [record.outcome for record in result.tool_calls] == ["executed"]
    assert result.stop_reason == "timed_out"
    assert result.steps == 1


@pytest.mark.asyncio
async def test_a_budget_that_has_not_expired_leaves_the_turn_alone() -> None:
    """A generous deadline changes nothing about a turn that finishes normally."""
    clock = FakeClock()
    provider = FakeProvider([AssistantTurn(text="all done")])

    result = await _loop(provider, deadline=Deadline(budget_s=30.0, _now=clock)).run("hi")

    assert result.stop_reason == "end_turn"
    assert result.text == "all done"


# --- the two stop reasons are distinct ----------------------------------------


@pytest.mark.asyncio
async def test_cancelled_and_timed_out_are_not_the_same_state() -> None:
    """They call for opposite responses, so they must not collapse into one spelling.

    A cancel is a decision somebody made and should be answered as such; a timeout is the
    service admitting it gave up. A caller that could not tell them apart would either retry a
    deliberate stop or apologise for one.
    """
    token = CancelToken()
    token.cancel("stop")

    cancelled: TurnResult = await _loop(FakeProvider([]), cancel=token).run("x")

    clock = FakeClock()
    expired = Deadline(budget_s=1.0, _now=clock)
    clock.advance(2.0)
    timed_out: TurnResult = await _loop(FakeProvider([]), deadline=expired).run("x")

    assert cancelled.stop_reason == "cancelled"
    assert timed_out.stop_reason == "timed_out"
    # Distinctness is established by naming both rather than by comparing them: the two are
    # distinct literals, so a comparison here is a check mypy has already made statically, and
    # it rejects the redundancy. Naming each is the assertion that carries information.


# --- the contract's view of the two new stop reasons --------------------------


def test_every_reason_the_loop_can_produce_has_a_translation() -> None:
    """A missing entry would report the turn as ``error``, which is a lie about why it stopped.

    This is the failure the map's comment warns about and that nothing catches by inspection: a
    key that does not match what the loop emits falls through to ``UNMAPPED_STOP_REASON``, so a
    turn that was cancelled or timed out would reach the wire claiming to have failed.
    """
    reasons_the_loop_can_emit = {
        "end_turn",
        "proposed",
        "max_steps",
        "timed_out",
        "cancelled",
    }

    assert reasons_the_loop_can_emit <= set(STOP_REASON_MAP), (
        "a stop reason the loop can emit has no translation and would be reported as an error"
    )


def test_a_timed_out_turn_is_reported_as_a_deadline_not_an_error() -> None:
    """The word the caller sees is the contract's, and giving up is not breaking.

    ``timed_out`` is the loop's own name for what happened to it; ``deadline`` is the
    contract's vocabulary for what a caller should do about it. Mapping one onto the other is
    the whole reason this table exists — and the case worth pinning, because the two names
    differ and a reader could reasonably assume they match.
    """
    result = TurnResult(text="I gave up on this turn", stop_reason="timed_out")

    response = to_invocation_response(result, session_id="sess-1")

    assert response.stop_reason == "deadline"
    # Not asserted against "error": the two are distinct literals, so mypy has already proven
    # they differ and rejects the comparison as redundant. Naming the expected value is the
    # assertion that carries information — and "deadline" is precisely the point, since an
    # unmapped reason would have produced "error" instead.


def test_a_cancelled_turn_reaches_the_wire_as_cancelled() -> None:
    """A decision somebody made, reported as such rather than folded into an error."""
    result = TurnResult(text="I stopped because the run was cancelled", stop_reason="cancelled")

    response = to_invocation_response(result, session_id="sess-1")

    assert response.stop_reason == "cancelled"
