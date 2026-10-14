"""Answering a proposal with a message, as `CONTRACT.md` says the platform does it.

The classifier's vocabulary is tested in `test_decisions.py`; what is tested here is the **seam**.
`CONTRACT.md` states that "the user's answer arrives as the next ``/invocations`` call in the same
session", and until this existed no code path read it that way — a "yes" was an ordinary message, the
write never ran, and nothing in the suite noticed because every approval case drove the decision
route instead.

So these tests are about three things, and none of them is the classifier:

* a message that *is* a decision reaches the same gate the button uses, with the turn's own session,
  and produces the same row transitions;
* a message that is *not* a decision changes nothing — the proposal stays exactly where it was, and
  the turn is answered normally, which is what makes a digression safe;
* a decision the gate refuses is reported as a sentence rather than raised, because the turn itself
  completed and a 409 in reply to a friendly "yes" would blame the user for their wording.

Hermetic: in-process, an in-memory database, a scripted provider and a spy transport. No server, no
MCP process, no trace directory outside `tmp_path`.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import agent_service.db.models  # noqa: F401 - registers every table on Base.metadata
from agent_service.agent.execution import WriteExecutor
from agent_service.agent.gate import ApprovalGate
from agent_service.agent.handler import DecisionApplier, InvocationHandlerFactory, _wire_record
from agent_service.agent.loop import LoopLimits, ToolOutcome
from agent_service.api.approval_routes import DecisionConflictError
from agent_service.api.models import InvocationRequest, ToolCallRecord
from agent_service.db.base import Base, create_session_factory, register_sqlite_pragmas
from agent_service.db.models.approval import ApprovalState
from agent_service.db.models.conversation import ChatSession
from agent_service.db.repositories.approvals import ApprovalRepository
from agent_service.providers.base import AssistantTurn, Message, ToolSchema
from agent_service.tools.registry import RegisteredTool, ToolRegistry
from tests.unit.test_approval_gate import SpyExecutor

SESSION_ID = "sess-decide-by-message"
USER_ID = "alice"
ACTION = "create_ticket"
PAYLOAD = {"title": "the printer is jammed again", "priority": "medium"}


class ScriptedProvider:
    """One fixed reply. The turn's *decision* is what these tests are about, not the model."""

    def __init__(self, text: str = "Here is what I found.") -> None:
        self._text = text
        self.turns = 0

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
        self.turns += 1
        return AssistantTurn(text=self._text)


class UnusedToolExecutor:
    """The loop's executor. Never reached: no test here lets the loop call a tool."""

    async def call(
        self, name: str, arguments: dict[str, Any], *, timeout_s: float | None = None
    ) -> ToolOutcome:
        raise AssertionError(f"the loop called {name}; these tests expect it not to")


def _registry() -> ToolRegistry:
    """A read tool and a write tool, with the write requiring approval as the real one does."""
    return ToolRegistry(
        [
            RegisteredTool(
                name="search_tickets",
                description="Search tickets",
                input_schema={"type": "object"},
                requires_approval=False,
            ),
            RegisteredTool(
                name=ACTION,
                description="Create a ticket",
                input_schema={"type": "object", "required": ["client_request_id"]},
                requires_approval=True,
            ),
        ]
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite pinned to one connection; see `test_approval_gate` for why."""
    built = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    register_sqlite_pragmas(built)
    Base.metadata.create_all(built)
    yield built
    built.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = create_session_factory(engine)
    with factory() as opened:
        opened.add(ChatSession(id=SESSION_ID, user_id=USER_ID))
        opened.commit()
        yield opened


@pytest.fixture
def spy() -> SpyExecutor:
    return SpyExecutor()


def _propose(session: Session) -> Any:
    return ApprovalRepository(session).propose(
        session_id=SESSION_ID,
        user_id=USER_ID,
        action=ACTION,
        payload=dict(PAYLOAD),
        client_request_id="key-message-1",
    )


def _applier(spy: SpyExecutor) -> DecisionApplier:
    """The same callable shape the wiring layer builds: gate in, decision result out.

    Built here rather than imported from `factory.py` because this is a unit test of the handler —
    the real one is exercised end to end by the eval suite, and the shape is the contract between
    the two.
    """

    async def decide(session: Session, approval_id: str, user_id: str, approved: bool) -> Any:
        gate = ApprovalGate.for_session(
            session,
            executor=WriteExecutor(executor=spy, approvals=ApprovalRepository(session)),
        )
        return await gate.decide(approval_id, user_id, approved)

    return decide


def _factory(
    tmp_path: Path,
    spy: SpyExecutor,
    *,
    decide: DecisionApplier | None,
    provider: ScriptedProvider | None = None,
) -> InvocationHandlerFactory:
    return InvocationHandlerFactory(
        provider=provider or ScriptedProvider(),
        tools=_registry(),
        executor=UnusedToolExecutor(),
        limits=LoopLimits(max_steps=2),
        trace_dir=tmp_path / "traces",
        turn_budget_s=5.0,
        decide=decide,
    )


async def _invoke(factory: InvocationHandlerFactory, prompt: str, *, session: Session) -> Any:
    request = InvocationRequest(prompt=prompt, user_id=USER_ID, session_id=SESSION_ID)
    response = await factory(request, session)
    session.commit()
    return response


# --- a message that decides ----------------------------------------------------


@pytest.mark.asyncio
async def test_an_affirmative_message_runs_the_write(
    session: Session, spy: SpyExecutor, tmp_path: Path
) -> None:
    """The contract's documented flow, end to end: propose, then "yes, go ahead".

    The assertions are the same three the decision route's own tests make — the row moved, the
    transport was called once with the *stored* payload, and the response says what happened —
    because it must be the same gate. A message that took a second path to the write would be the
    defect this test exists to catch.
    """
    approval = _propose(session)
    factory = _factory(tmp_path, spy, decide=_applier(spy))

    response = await _invoke(factory, "yes, go ahead", session=session)

    assert response.stop_reason == "completed"
    assert response.pending_approval is None
    assert [call.outcome for call in response.tool_calls] == ["executed"]
    assert spy.names == [ACTION], "the write ran exactly once, through the gate"

    name, sent = spy.calls[0]
    assert name == ACTION
    assert sent["title"] == PAYLOAD["title"], "the executed arguments are the stored ones"
    assert sent["client_request_id"] == "key-message-1"

    found = ApprovalRepository(session).get(approval.id, user_id=USER_ID)
    assert found is not None
    assert found.state == ApprovalState.EXECUTED.value


@pytest.mark.asyncio
async def test_a_refusing_message_declines_and_writes_nothing(
    session: Session, spy: SpyExecutor, tmp_path: Path
) -> None:
    """The other half, by message: a person said no, so nothing reached the backend."""
    approval = _propose(session)
    factory = _factory(tmp_path, spy, decide=_applier(spy))

    response = await _invoke(factory, "no thanks", session=session)

    assert [call.outcome for call in response.tool_calls] == ["refused"]
    assert spy.calls == [], "a declined proposal must not reach the transport"
    assert "declined" in response.output, "the user is told their no was recorded"

    found = ApprovalRepository(session).get(approval.id, user_id=USER_ID)
    assert found is not None
    assert found.state == ApprovalState.DECLINED.value


@pytest.mark.asyncio
async def test_every_decision_is_reported_with_a_duration_field(
    session: Session, spy: SpyExecutor, tmp_path: Path
) -> None:
    """A decision is reported as a tool call, so it carries the contract's keys like any other.

    Not a cosmetic check: a client rendering a run reads one list of calls, and a decision that
    arrived through this path must be indistinguishable from one made by pressing the button —
    including the ``input``/``status`` names the contract's example response uses.
    """
    _propose(session)
    factory = _factory(tmp_path, spy, decide=_applier(spy))

    response = await _invoke(factory, "approve", session=session)

    dumped = response.tool_calls[0].model_dump()
    assert set(dumped) >= {
        "name",
        "arguments",
        "input",
        "outcome",
        "status",
        "duration_ms",
        "is_error",
    }
    assert dumped["input"] == dumped["arguments"] == PAYLOAD
    assert dumped["status"] == dumped["outcome"] == "executed"


# --- a message that does not decide -------------------------------------------


@pytest.mark.asyncio
async def test_an_unrelated_message_leaves_the_proposal_pending(
    session: Session, spy: SpyExecutor, tmp_path: Path
) -> None:
    """A digression must not consume the proposal, and must not silently hide it either.

    The turn is answered normally — that is what "what to do if the user asks something unrelated"
    means — and the still-outstanding proposal is carried on the response, so a client that renders
    ``pending_approval`` keeps showing the user what is waiting for them.
    """
    approval = _propose(session)
    provider = ScriptedProvider("These are the tickets I found.")
    factory = _factory(tmp_path, spy, decide=_applier(spy), provider=provider)

    response = await _invoke(factory, "find tickets about the printer", session=session)

    assert provider.turns == 1, "the message was answered as a turn, not as a decision"
    assert response.output == "These are the tickets I found."
    assert response.pending_approval is not None
    assert response.pending_approval.approval_id == approval.id
    assert response.pending_approval.action == ACTION, "with the contract's key populated too"
    assert spy.calls == []

    found = ApprovalRepository(session).get(approval.id, user_id=USER_ID)
    assert found is not None
    assert found.state == ApprovalState.PENDING.value


@pytest.mark.asyncio
async def test_without_an_applier_an_affirmative_message_is_just_a_message(
    session: Session, spy: SpyExecutor, tmp_path: Path
) -> None:
    """The seam is optional, and its absence is safe rather than dangerous.

    With no gate installed the handler must behave exactly as it did before this existed: the
    message is answered, the proposal is untouched, and no write can happen. A seam that failed
    open — approving something because nothing was wired to approve it — would be far worse than one
    that does nothing.
    """
    approval = _propose(session)
    factory = _factory(tmp_path, spy, decide=None)

    response = await _invoke(factory, "yes, go ahead", session=session)

    assert response.pending_approval is not None
    assert spy.calls == []
    found = ApprovalRepository(session).get(approval.id, user_id=USER_ID)
    assert found is not None
    assert found.state == ApprovalState.PENDING.value


# --- a decision the gate refuses ----------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_decision_is_reported_as_a_sentence(
    session: Session, spy: SpyExecutor, tmp_path: Path
) -> None:
    """A 409 would blame the user's wording for a state problem, so it is not used here.

    The race this models is real and narrow: the proposal was pending when the handler looked, and
    decided or expired by the time the gate was asked. What matters is that the user is told, in
    the conversation, that the answer arrived too late — and that nothing was written.
    """

    async def refusing(_session: Session, _approval_id: str, _user_id: str, _approved: bool) -> Any:
        raise DecisionConflictError("approval 'a1' is 'declined' and cannot be decided again")

    _propose(session)
    factory = _factory(tmp_path, spy, decide=refusing)

    response = await _invoke(factory, "yes", session=session)

    assert response.stop_reason == "completed"
    assert [call.outcome for call in response.tool_calls] == ["refused"]
    assert "can no longer be decided" in response.output
    assert spy.calls == []


def test_the_wire_record_carries_both_vocabularies() -> None:
    """The single builder, checked directly for the pairing the contract and this service share.

    Asserted here rather than only through a response because the pairing is the whole reason the
    function exists: ``input``/``arguments`` and ``status``/``outcome`` are one fact under two
    names, and a response that filled one pair in and not the other would still type-check.
    """
    record: ToolCallRecord = _wire_record(
        name=ACTION,
        arguments=dict(PAYLOAD),
        outcome="proposed",
        detail="awaiting approval a1",
        duration_ms=7,
    )

    assert record.input == record.arguments == PAYLOAD
    assert record.status == record.outcome == "proposed"
    assert record.duration_ms == 7
    assert record.is_error is False, "a proposal is not a failure"
