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
from agent_service.agent.loop import AgentLoop, LoopLimits, ToolOutcome, TurnResult
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
    """Echoes each call, counts the ones that happened, and records the slice it was given.

    ``slices`` is the point of the ``timeout_s`` parameter existing at all: it lets a test assert
    what the turn's budget actually granted a call, rather than only that the call happened.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.slices: list[float | None] = []
        #: Run *inside* the call, so a test can burn the budget while a step is in flight -
        #: which is the only way to model the situation the loop must not interrupt.
        self.on_call: Any = None
        #: Tools this fake reports as failed, so a test can drive the branch where the loop must
        #: record a call that ran but produced no answer.
        self.failing: set[str] = set()

    async def call(
        self, name: str, arguments: dict[str, Any], *, timeout_s: float | None = None
    ) -> ToolOutcome:
        if self.on_call is not None:
            self.on_call()
        self.calls.append(name)
        self.slices.append(timeout_s)
        failed = name in self.failing
        return ToolOutcome(
            text=f"ERROR: {name} failed" if failed else f"{name} ran",
            is_error=failed,
        )


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
    spans: Any = None,
    approvals: Any = None,
    writes: Sequence[str] = (),
) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        tools=_registry(writes=writes),
        executor=executor or FakeExecutor(),
        approvals=cast(ApprovalRepository, approvals or UnusedApprovals()),
        session_id="sess-1",
        user_id="alice",
        limits=LoopLimits(max_steps=max_steps),
        deadline=deadline,
        cancel=cancel,
        spans=spans,
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
    # Burned *inside* the call, which is the scenario that matters: the budget was fine when the
    # step began, and is gone by the time it ends. Burning it during the provider reply instead
    # would make the grant refuse the call before it started, which is correct behaviour but a
    # different case — and it would leave the "in flight" claim untested.
    executor.on_call = lambda: clock.advance(99.0)

    result = await _loop(provider, executor=executor, deadline=deadline).run("find it")

    assert executor.calls == ["search_tickets"], "the step in flight was allowed to complete"
    assert [record.outcome for record in result.tool_calls] == ["executed"]
    assert result.stop_reason == "timed_out"
    assert result.steps == 1, "the step that began is the only one counted"


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


# --- the granted slice --------------------------------------------------------


@pytest.mark.asyncio
async def test_the_slice_granted_to_a_call_is_bounded_by_the_turn_budget() -> None:
    """The budget bounds a *single* call, not only the gaps between them.

    Without this, one tool that never returns would hold the turn open forever: the next
    checkpoint is never reached, so nothing would ever notice the budget was gone. The grant is
    what closes that hole, and asserting it here is what keeps it closed — the parameter could be
    dropped from the call site and every other test in this file would still pass.
    """
    clock = FakeClock()
    deadline = Deadline(budget_s=30.0, step_ceiling_s=10.0, _now=clock)
    provider = FakeProvider([_read_call()])
    executor = FakeExecutor()

    result = await _loop(provider, executor=executor, deadline=deadline).run("find it")

    assert len(executor.slices) == 1, "one call, one grant"
    granted = executor.slices[0]
    assert granted is not None
    # The step ceiling is smaller than what the turn has left, so the ceiling wins.
    assert granted <= 10.0, "a call may not be granted more than the step ceiling"
    assert granted > 0.0, "a call with time left must be granted a positive slice"
    # Two steps: the one that ran the tool, and the one that answered with what it found. Only
    # the first grants a slice, which is what the assertion above pins.
    assert result.steps == 2


@pytest.mark.asyncio
async def test_a_loop_with_no_deadline_grants_no_slice() -> None:
    """The executor's own ceiling applies when the turn has no budget.

    A test that drives the loop with no clock should not be handed a fabricated slice, and the
    adapter already has a sensible default. Passing ``None`` is how the loop says "not mine to
    decide", rather than inventing a number.
    """
    executor = FakeExecutor()

    await _loop(FakeProvider([_read_call()]), executor=executor).run("find it")

    assert executor.slices == [None]


# --- what the loop records ----------------------------------------------------


class RecordingSpan:
    """One span as this test sees it: the attributes are the point, so they are a plain dict."""

    def __init__(self, kind: str, name: str, attributes: dict[str, Any]) -> None:
        self.kind = kind
        self.name = name
        self.attributes = attributes

    def __enter__(self) -> RecordingSpan:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        """Returns nothing, so an exception inside a recorded block still propagates.

        A recorder that swallowed an exception to keep its own bookkeeping tidy would hide the
        failure from the loop, which is the opposite of what a recorder is for.
        """


class RecordingSpans:
    """A `SpanRecorder` that keeps everything, so a test can assert on what reached it.

    Hand-written rather than a `Tracer` because a trace directory and a JSONL file are not what is
    being tested: the loop must record the right *facts*, and where they are written afterwards is
    the tracer's own concern, covered by `test_observability.py`.
    """

    def __init__(self) -> None:
        self.spans: list[RecordingSpan] = []
        self.events: list[dict[str, Any]] = []

    def span(self, kind: str, name: str, **attributes: Any) -> RecordingSpan:
        opened = RecordingSpan(kind, name, dict(attributes))
        self.spans.append(opened)
        return opened

    def add(self, kind: str, name: str, **attributes: Any) -> None:
        self.events.append({"kind": kind, "name": name, **attributes})


class RecordingApprovals:
    """A repository that records proposals instead of storing them.

    The loop only reads ``id``, ``action``, ``payload`` and ``state`` off a proposal, so a real row
    would prove nothing extra here — the database is exercised by `test_approval_gate.py` and by
    every eval case that proposes a write.
    """

    def __init__(self) -> None:
        self.proposed: list[dict[str, Any]] = []

    def propose(self, **fields: Any) -> Any:
        self.proposed.append(fields)
        return RecordingProposal(fields)


class RecordingProposal:
    def __init__(self, fields: dict[str, Any]) -> None:
        self.id = f"approval-{len(fields.get('action', ''))}"
        self.action = str(fields.get("action", ""))
        self.payload = dict(fields.get("payload", {}))
        self.state = "pending"


def _write_call(name: str = "create_ticket") -> AssistantTurn:
    """A reply that asks for one write, which the loop must propose rather than perform."""
    return AssistantTurn(
        text="I will open a ticket for that.",
        tool_calls=(
            ToolCallRequest(
                id="call-w",
                name=name,
                arguments={"title": "a title", "client_request_id": "key-1"},
            ),
        ),
        stop_reason="tool_use",
    )


@pytest.mark.asyncio
async def test_a_tool_call_is_recorded_with_its_arguments_and_outcome() -> None:
    """The requirement the trace could not previously satisfy: what was called, and with what.

    Asserted at the loop rather than at the trace file because the loop is what knows — the file is
    only where it ends up. A tool call that reached the trace without its arguments would answer
    "something was searched" rather than "the printer was searched", which is the question anyone
    opens a trace with.
    """
    spans = RecordingSpans()
    executor = FakeExecutor()

    result = await _loop(FakeProvider([_read_call()]), executor=executor, spans=spans).run(
        "find it"
    )

    assert [record.kind for record in spans.spans] == ["tool_call"]
    recorded = spans.spans[0]
    assert recorded.name == "search_tickets"
    assert recorded.attributes["arguments"] == {"q": "printer"}
    assert recorded.attributes["outcome"] == "executed"
    assert recorded.attributes["is_error"] is False
    # And the same measurement reaches the response, because a caller reading only the response
    # should not have to open a trace file to find which call was slow.
    assert result.tool_calls[0].duration_ms is not None
    assert result.tool_calls[0].duration_ms >= 0


@pytest.mark.asyncio
async def test_a_failed_call_is_recorded_as_failed() -> None:
    """The refusal is recorded as a refusal, which is the fact the trace exists to preserve.

    A span that recorded "tool_call search_tickets" with no outcome would make a search that the
    backend rejected indistinguishable from one that returned rows — the same confusion the
    response's ``is_error`` was introduced to fix, one layer down.
    """
    spans = RecordingSpans()
    executor = FakeExecutor()
    executor.failing = {"search_tickets"}

    await _loop(FakeProvider([_read_call()]), executor=executor, spans=spans).run("find it")

    recorded = spans.spans[0]
    assert recorded.attributes["outcome"] == "failed"
    assert recorded.attributes["is_error"] is True


@pytest.mark.asyncio
async def test_a_proposal_is_recorded_as_an_event_with_the_arguments() -> None:
    """What was proposed, and with what — recorded at the moment it became a proposal.

    The row is the authority on what will run, and the span is the record of what the *agent* asked
    for. Keeping both means a later "this is not what I approved" question can be answered by
    comparing two artefacts rather than by re-running the turn.
    """
    spans = RecordingSpans()
    approvals = RecordingApprovals()

    result = await _loop(
        FakeProvider([_write_call()]),
        spans=spans,
        approvals=approvals,
        writes=["create_ticket"],
    ).run("open a ticket")

    assert result.stop_reason == "proposed"
    assert spans.spans == [], "a proposal is not a call: nothing was executed"
    assert [event["kind"] for event in spans.events] == ["proposal"]
    assert spans.events[0]["name"] == "create_ticket"
    assert spans.events[0]["arguments"]["title"] == "a title"
    assert spans.events[0]["state"] == "pending"
    assert approvals.proposed[0]["payload"]["client_request_id"] == "key-1"


@pytest.mark.asyncio
async def test_a_loop_with_no_recorder_still_times_its_calls() -> None:
    """The recorder is optional; the measurement is not.

    These are deliberately separate, and this test is what keeps them separate: recording is about
    observability being installed, while ``duration_ms`` is part of the response the contract
    describes. A run with no tracer must still tell its caller how long each call took.
    """
    result = await _loop(FakeProvider([_read_call()])).run("find it")

    assert result.tool_calls[0].duration_ms is not None
