"""The approval gate, end to end: propose, decide, execute, and refuse a second execution.

This is the suite the assignment's scoring criteria point at, so it is written to prove the
*gate* rather than to exercise the classes. Every test below is about one of four claims:

1. A write the model asks for is **not performed**. It becomes a row.
2. Execution cannot happen until a human approves, and the guard is not in the caller.
3. What is executed is **what was approved** — the stored payload, with the stored key.
4. The same approval cannot create two tickets, even executed twice, even after a restart.

Hermetic: an in-memory SQLite database and a scripted transport. No MCP server, no model, no
network. The transport is a spy, so "the tool was not called" is asserted rather than inferred.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import agent_service.db.models  # noqa: F401 - registers every table on Base.metadata
from agent_service.agent.execution import ToolCallFailedError, WriteExecutor
from agent_service.agent.loop import AgentLoop, LoopLimits
from agent_service.db.base import Base, create_session_factory, register_sqlite_pragmas
from agent_service.db.models.approval import ApprovalState
from agent_service.db.models.conversation import ChatSession
from agent_service.db.repositories.approvals import (
    ApprovalRepository,
    ApprovalTransitionError,
)
from agent_service.mcp.client import McpTool
from agent_service.providers.scripted import ScriptedProvider
from agent_service.tools.registry import ToolRegistry

SESSION_ID = "session-1"
USER_ID = "user-1"
OTHER_USER = "user-2"

#: A tool schema shaped exactly as the MCP server publishes a write: the required
#: ``client_request_id`` is what makes it a write, per Stage 5 and Stage 6.
WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_request_id": {"type": "string"},
        "title": {"type": "string"},
    },
    "required": ["client_request_id", "title"],
}

READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"q": {"type": "string"}},
}


def _tool(name: str, *, write: bool) -> McpTool:
    """A published tool. ``is_write`` is what the real client derives from the schema."""
    return McpTool(
        name=name,
        description=f"{name} (test)",
        input_schema=WRITE_SCHEMA if write else READ_SCHEMA,
    )


class SpyExecutor:
    """Records every call it is asked to make, and answers with scripted text.

    A spy rather than a stub because half of these tests assert on what was *not* called —
    "the write did not happen" is only provable if the transport can report its calls.
    """

    def __init__(self, *, reply: str = "3 of 3 found") -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reply = reply
        self.raises: Exception | None = None
        #: When set, ``call_checked`` reports a *completed* call that the backend refused —
        #: distinct from ``raises``, which models a transport that never delivered the call.
        self.reports_failure = False

    async def call(
        self, name: str, arguments: dict[str, Any], *, timeout_s: float | None = None
    ) -> str:
        """The loop's variant. ``timeout_s`` is the slice the turn had left, when it had a budget.

        Accepted and ignored here: this suite is about the approval gate, not about the clock, and
        the parameter is part of the executor protocol so the spy has to match its shape.
        """
        if self.raises is not None:
            raise self.raises
        self.calls.append((name, arguments))
        return self.reply

    async def call_checked(self, name: str, arguments: dict[str, Any]) -> str:
        """The writer's variant: records the call, then raises if the backend refused it.

        The two methods exist because one result has two readers. ``call`` renders a failure as
        text because the loop has to *show* a model what went wrong; the writer must not be able
        to read that same text as a success, so the adapter it is given raises instead. Without
        this method the spy does not satisfy what the writer asks for, which is how the tests
        caught the change — and ``reports_failure`` is what lets them prove a refused write ends
        up ``failed`` rather than ``executed``.
        """
        if self.raises is not None:
            raise self.raises
        self.calls.append((name, arguments))
        if self.reports_failure:
            raise ToolCallFailedError(self.reply)
        return self.reply

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def engine() -> Iterator[Engine]:
    """An in-memory database with the real schema.

    Built here rather than with ``create_db_engine`` because an in-memory SQLite database only
    exists for as long as its connection does, and the default pool hands out a *new* connection
    per checkout — so ``create_all`` would run against one connection while every later query
    ran against a different, empty database. ``StaticPool`` pins the database to a single shared
    connection, which is what makes ``:memory:`` behave like a database at all.

    ``create_all`` rather than migrations: migrations are the subject of Stage 2's tests, and
    re-running them here would make an approval failure look like a migration failure.
    """
    built = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # The same pragmas production uses, so foreign keys are actually enforced here too.
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
def approvals(session: Session) -> ApprovalRepository:
    return ApprovalRepository(session)


@pytest.fixture
def executor() -> SpyExecutor:
    return SpyExecutor()


# --- 1. a requested write is recorded, not performed --------------------------


async def test_a_write_the_model_requests_is_not_called(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """The claim the whole design rests on, asserted against the transport itself.

    The scripted provider asks for ``create_ticket`` because that is what it does with "open a
    ticket". The executor's call list must be empty afterwards: not "the result was discarded",
    but "the call never happened".
    """
    loop = AgentLoop(
        provider=ScriptedProvider(),
        tools=_registry(),
        executor=executor,
        approvals=approvals,
        session_id=SESSION_ID,
        user_id=USER_ID,
    )

    result = await loop.run("open a ticket about the printer")

    assert executor.calls == [], "a proposed write must not reach the transport"
    assert result.awaits_approval
    assert result.stop_reason == "proposed"


async def test_the_proposal_is_persisted_as_pending(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """It must survive a restart, which means it is a row rather than an object."""
    loop = AgentLoop(
        provider=ScriptedProvider(),
        tools=_registry(),
        executor=executor,
        approvals=approvals,
        session_id=SESSION_ID,
        user_id=USER_ID,
    )

    result = await loop.run("open a ticket about the printer")
    approval = result.pending[0]

    reloaded = approvals.get(approval.id, user_id=USER_ID)
    assert reloaded is not None
    assert reloaded.state == ApprovalState.PENDING.value
    assert reloaded.action == "create_ticket"


async def test_the_stored_payload_is_the_models_arguments(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """Everything needed to execute is on the row, so nothing is re-derived later."""
    loop = AgentLoop(
        provider=ScriptedProvider(),
        tools=_registry(),
        executor=executor,
        approvals=approvals,
        session_id=SESSION_ID,
        user_id=USER_ID,
    )

    approval = (await loop.run("open a ticket about the printer")).pending[0]

    assert approval.payload["title"] == "about the printer"
    assert approval.payload["client_request_id"] == "scripted-create-1"


async def test_a_read_is_executed_without_asking(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """The gate must not block reads, or the agent would be useless rather than safe."""
    loop = AgentLoop(
        provider=ScriptedProvider(),
        tools=_registry(),
        executor=executor,
        approvals=approvals,
        session_id=SESSION_ID,
        user_id=USER_ID,
    )

    result = await loop.run("find tickets about the printer")

    assert executor.names == ["search_tickets"]
    assert not result.awaits_approval


# --- 2. execution requires approval, and the guard is not in the caller -------


async def test_a_pending_write_cannot_be_executed(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """Executing an undecided row is exactly the write-without-consent the table prevents."""
    approval = approvals.propose(
        session_id=SESSION_ID, user_id=USER_ID, action="create_ticket", payload={"title": "x"}
    )
    writer = WriteExecutor(executor=executor, approvals=approvals)

    with pytest.raises(ApprovalTransitionError, match="has not been decided"):
        await writer.execute(approval.id, user_id=USER_ID)

    assert executor.calls == []


async def test_a_declined_write_cannot_be_executed(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    approval = approvals.propose(
        session_id=SESSION_ID, user_id=USER_ID, action="create_ticket", payload={"title": "x"}
    )
    approvals.decide(approval.id, user_id=USER_ID, approved=False)
    writer = WriteExecutor(executor=executor, approvals=approvals)

    with pytest.raises(ApprovalTransitionError, match="only an approved one may run"):
        await writer.execute(approval.id, user_id=USER_ID)

    assert executor.calls == []


async def test_an_approved_write_is_executed(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    approval = approvals.propose(
        session_id=SESSION_ID,
        user_id=USER_ID,
        action="create_ticket",
        payload={"title": "about the printer"},
        client_request_id="key-1",
    )
    approvals.decide(approval.id, user_id=USER_ID, approved=True)
    writer = WriteExecutor(executor=executor, approvals=approvals)

    outcome = await writer.execute(approval.id, user_id=USER_ID)

    assert outcome.succeeded
    assert executor.names == ["create_ticket"]


async def test_another_user_cannot_decide_or_execute(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """User-scoping is a parameter, so a foreign id is indistinguishable from a bad one."""
    approval = approvals.propose(
        session_id=SESSION_ID, user_id=USER_ID, action="create_ticket", payload={"title": "x"}
    )

    assert approvals.get(approval.id, user_id=OTHER_USER) is None
    with pytest.raises(ApprovalTransitionError, match="for this user"):
        approvals.decide(approval.id, user_id=OTHER_USER, approved=True)

    assert executor.calls == []


async def test_a_proposal_cannot_be_decided_twice(
    session: Session, approvals: ApprovalRepository
) -> None:
    """Otherwise a refusal could be flipped to an approval after the fact."""
    approval = approvals.propose(
        session_id=SESSION_ID, user_id=USER_ID, action="create_ticket", payload={"title": "x"}
    )
    approvals.decide(approval.id, user_id=USER_ID, approved=False)

    with pytest.raises(ApprovalTransitionError, match="cannot be decided again"):
        approvals.decide(approval.id, user_id=USER_ID, approved=True)


async def test_an_expired_proposal_is_refused_and_marked(
    session: Session, approvals: ApprovalRepository
) -> None:
    """A stale approval executed late is worse than a refused one, so it is refused."""
    past = datetime.now(UTC) - timedelta(hours=2)
    approval = approvals.propose(
        session_id=SESSION_ID,
        user_id=USER_ID,
        action="create_ticket",
        payload={"title": "x"},
        ttl_seconds=60,
        now=past,
    )

    with pytest.raises(ApprovalTransitionError, match="expired"):
        approvals.decide(approval.id, user_id=USER_ID, approved=True)

    assert _state_of(approvals, approval.id) == ApprovalState.EXPIRED.value


async def test_an_expired_proposal_is_not_offered_as_decidable(
    session: Session, approvals: ApprovalRepository
) -> None:
    """The hot query must not hand a caller something it cannot act on."""
    approvals.propose(
        session_id=SESSION_ID,
        user_id=USER_ID,
        action="create_ticket",
        payload={"title": "x"},
        ttl_seconds=60,
        now=datetime.now(UTC) - timedelta(hours=2),
    )

    assert approvals.pending(session_id=SESSION_ID, user_id=USER_ID) == ()


# --- 3. what runs is what was approved ---------------------------------------


async def test_execution_sends_the_stored_payload(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """Approve one ticket, get that ticket — even if nothing else is available to re-derive it.

    The payload is deliberately *not* what a fresh model call would produce; it is written to
    the row by hand, so a passing test proves the stored value was used rather than a
    coincidence of the scripted provider being deterministic.
    """
    approval = approvals.propose(
        session_id=SESSION_ID,
        user_id=USER_ID,
        action="create_ticket",
        payload={
            "client_request_id": "ignored-on-purpose",
            "title": "the exact title that was approved",
        },
        client_request_id="row-key-1",
    )
    approvals.decide(approval.id, user_id=USER_ID, approved=True)

    await WriteExecutor(executor=executor, approvals=approvals).execute(
        approval.id, user_id=USER_ID
    )

    _name, sent = executor.calls[0]
    assert sent["title"] == "the exact title that was approved"


async def test_execution_sends_the_rows_key_not_the_payloads(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """The row is the authority on which key this approval is for.

    The payload deliberately carries a *different* key. The payload came from a model, and a
    model that supplied a fresh key on a retry would break the replay guarantee — so the row
    wins, and this test is what says so.
    """
    approval = approvals.propose(
        session_id=SESSION_ID,
        user_id=USER_ID,
        action="create_ticket",
        payload={"client_request_id": "a-stale-or-wrong-key", "title": "x"},
        client_request_id="the-rows-key",
    )
    approvals.decide(approval.id, user_id=USER_ID, approved=True)

    await WriteExecutor(executor=executor, approvals=approvals).execute(
        approval.id, user_id=USER_ID
    )

    _name, sent = executor.calls[0]
    assert sent["client_request_id"] == "the-rows-key"


# --- 4. one approval, one ticket ---------------------------------------------


async def test_executing_twice_does_not_call_the_tool_twice(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """The row is terminal after execution, so a retry is refused rather than repeated.

    This is the in-process half of the guarantee. The other half — a *restart* between the two
    attempts — is covered by the key test below, because after a restart the row is still
    ``executed`` and the refusal comes from the same place.
    """
    approval = approvals.propose(
        session_id=SESSION_ID,
        user_id=USER_ID,
        action="create_ticket",
        payload={"title": "x"},
        client_request_id="key-1",
    )
    approvals.decide(approval.id, user_id=USER_ID, approved=True)
    writer = WriteExecutor(executor=executor, approvals=approvals)

    first = await writer.execute(approval.id, user_id=USER_ID)
    with pytest.raises(ApprovalTransitionError):
        await writer.execute(approval.id, user_id=USER_ID)

    assert first.succeeded
    assert executor.names == ["create_ticket"], "the tool ran exactly once"


async def test_the_key_is_minted_at_proposal_time(
    session: Session, approvals: ApprovalRepository
) -> None:
    """The property that makes a post-restart retry safe.

    The key exists on the row *before* anyone approves anything, so a crash between approval and
    execution cannot cause a fresh key to be used for the same intent. A key minted inside the
    executor would be new on every attempt, and every single-execution test would still pass.
    """
    first = approvals.propose(
        session_id=SESSION_ID, user_id=USER_ID, action="create_ticket", payload={"title": "x"}
    )
    approvals.decide(first.id, user_id=USER_ID, approved=True)

    # A second, unrelated proposal must not share the key.
    second = approvals.propose(
        session_id=SESSION_ID, user_id=USER_ID, action="create_ticket", payload={"title": "y"}
    )

    assert first.client_request_id
    assert second.client_request_id != first.client_request_id


async def test_a_replay_is_reported_without_claiming_a_new_ticket(
    session: Session, approvals: ApprovalRepository
) -> None:
    """Stage 4's ledger signal must survive to the report, or a user is told a lie.

    The backend answered "already done". The outcome is a success, but the message must not
    imply a ticket was created by this call.
    """
    approval = approvals.propose(
        session_id=SESSION_ID,
        user_id=USER_ID,
        action="create_ticket",
        payload={"title": "x"},
        client_request_id="key-1",
    )
    approvals.decide(approval.id, user_id=USER_ID, approved=True)
    replaying = SpyExecutor(reply="ticket TCK-1 (replayed, previously created)")

    outcome = await WriteExecutor(executor=replaying, approvals=approvals).execute(
        approval.id, user_id=USER_ID
    )

    assert outcome.succeeded
    assert outcome.replayed
    assert "no second ticket was created" in outcome.detail


async def test_a_failing_write_is_recorded_not_raised(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """A failure must become a state, or the row stays approved and is retried forever."""
    approval = approvals.propose(
        session_id=SESSION_ID,
        user_id=USER_ID,
        action="create_ticket",
        payload={"title": "x"},
        client_request_id="key-1",
    )
    approvals.decide(approval.id, user_id=USER_ID, approved=True)
    executor.raises = RuntimeError("the ticketing API is unreachable")

    outcome = await WriteExecutor(executor=executor, approvals=approvals).execute(
        approval.id, user_id=USER_ID
    )

    assert not outcome.succeeded
    assert outcome.outcome == "failed"
    assert _state_of(approvals, approval.id) == ApprovalState.FAILED.value


async def test_a_failed_write_is_not_retried_on_the_next_attempt(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """Because the state is terminal, a sweep cannot pick it up and try again."""
    approval = approvals.propose(
        session_id=SESSION_ID, user_id=USER_ID, action="create_ticket", payload={"title": "x"}
    )
    approvals.decide(approval.id, user_id=USER_ID, approved=True)
    executor.raises = RuntimeError("boom")
    writer = WriteExecutor(executor=executor, approvals=approvals)
    await writer.execute(approval.id, user_id=USER_ID)

    executor.raises = None
    with pytest.raises(ApprovalTransitionError):
        await writer.execute(approval.id, user_id=USER_ID)


# --- the sweep -----------------------------------------------------------------


def test_the_sweep_expires_only_overdue_rows(
    session: Session, approvals: ApprovalRepository
) -> None:
    overdue = approvals.propose(
        session_id=SESSION_ID,
        user_id=USER_ID,
        action="create_ticket",
        payload={"title": "old"},
        ttl_seconds=60,
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    live = approvals.propose(
        session_id=SESSION_ID, user_id=USER_ID, action="create_ticket", payload={"title": "new"}
    )

    changed = approvals.expire_stale()

    assert changed == 1
    assert _state_of(approvals, overdue.id) == ApprovalState.EXPIRED.value
    assert _state_of(approvals, live.id) == ApprovalState.PENDING.value


def test_the_sweep_is_idempotent(session: Session, approvals: ApprovalRepository) -> None:
    approvals.propose(
        session_id=SESSION_ID,
        user_id=USER_ID,
        action="create_ticket",
        payload={"title": "old"},
        ttl_seconds=60,
        now=datetime.now(UTC) - timedelta(hours=1),
    )

    assert approvals.expire_stale() == 1
    assert approvals.expire_stale() == 0


# --- the loop's own edges -----------------------------------------------------


async def test_the_step_ceiling_is_reported_rather_than_raised(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """A model that keeps calling tools is cut off; that is a fact about the turn."""

    class EndlessProvider:
        """Always wants a tool, never answers — the shape the ceiling exists for."""

        name = "scripted"

        async def chat(self, messages: Any, tools: Any = (), *, budget: Any = None) -> Any:
            from agent_service.providers.base import AssistantTurn, ToolCallRequest

            return AssistantTurn(
                text="again",
                tool_calls=(ToolCallRequest(id="c1", name="search_tickets", arguments={"q": "x"}),),
                stop_reason="tool_use",
            )

    loop = AgentLoop(
        provider=EndlessProvider(),  # type: ignore[arg-type]
        tools=_registry(),
        executor=executor,
        approvals=approvals,
        session_id=SESSION_ID,
        user_id=USER_ID,
        limits=LoopLimits(max_steps=3),
    )

    result = await loop.run("loop forever")

    assert result.stop_reason == "max_steps"
    assert len(executor.calls) == 3


async def test_a_transport_failure_becomes_a_readable_result(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """The model must be told why, and told not to retry unchanged."""
    executor.raises = ConnectionError("no route to host")
    loop = AgentLoop(
        provider=ScriptedProvider(),
        tools=_registry(),
        executor=executor,
        approvals=approvals,
        session_id=SESSION_ID,
        user_id=USER_ID,
    )

    result = await loop.run("find tickets about the printer")

    assert result.stop_reason == "end_turn", "the run finishes rather than raising"
    assert "ERROR" in result.text or "not going to keep retrying" in result.text


# --- helpers -------------------------------------------------------------------


def _state_of(approvals: ApprovalRepository, approval_id: str) -> str:
    """The stored state of a row, asserting it exists.

    A helper because ``get`` returns ``None`` for a row that is missing *or* not this user's,
    and reading ``.state`` off it directly would pass at runtime and be a type error — the
    optional is doing real work here, so it is asserted rather than ignored. Four call sites
    would otherwise repeat the same assertion.
    """
    approval = approvals.get(approval_id, user_id=USER_ID)
    assert approval is not None, f"approval {approval_id} should exist for {USER_ID}"
    return approval.state


def _registry() -> ToolRegistry:
    """The four tools, with the write one marked by its schema as the real client would."""
    return ToolRegistry.from_discoverable(
        _Published(
            (
                _tool("search_tickets", write=False),
                _tool("get_ticket", write=False),
                _tool("create_ticket", write=True),
                _tool("add_comment", write=True),
            )
        )
    )


class _Published:
    """The ``_Discoverable`` shape: anything with a ``tools`` tuple."""

    def __init__(self, tools: tuple[McpTool, ...]) -> None:
        self._tools = tools

    @property
    def tools(self) -> tuple[McpTool, ...]:
        return self._tools


async def test_a_reported_failure_is_not_recorded_as_executed(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """The regression this suite was missing, and the reason ``call_checked`` exists.

    Every other failure test here scripted a *raised* exception — a transport that never
    delivered the call. This one scripts the harder case: the call arrived, the backend answered,
    and the answer was a failure. The tool executor's loop-facing ``call`` renders that as the
    text ``ERROR: ...``, and the writer used to read the same string and mark the row
    ``executed``, because nothing in the value said otherwise.

    So the row claimed a ticket existed that did not. It was found by running the assembled
    service: an approved ``create_ticket`` against a healthy mock came back with a get-ticket
    error and a state of ``executed``, and the ``ERROR:`` prefix in the detail was the adapter's
    own marker — which is how the lost signal was identified.
    """
    approval = approvals.propose(
        session_id=SESSION_ID,
        user_id=USER_ID,
        action="create_ticket",
        payload={"title": "x"},
        client_request_id="key-1",
    )
    approvals.decide(approval.id, user_id=USER_ID, approved=True)
    executor.reports_failure = True
    executor.reply = "No ticket with that id exists. Check the id before retrying."

    outcome = await WriteExecutor(executor=executor, approvals=approvals).execute(
        approval.id, user_id=USER_ID
    )

    assert not outcome.succeeded, "a refusal must never be reported as a completed write"
    assert outcome.outcome == "failed"
    assert "No ticket with that id exists" in outcome.detail
    assert _state_of(approvals, approval.id) == ApprovalState.FAILED.value


async def test_a_reported_failure_does_not_leave_the_row_approved(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """The consequence that matters operationally: an approved row is retried by every sweep.

    Left ``approved``, a write that failed would be re-attempted indefinitely, which is how one
    refusal becomes a stream of duplicate attempts.
    """
    approval = approvals.propose(
        session_id=SESSION_ID,
        user_id=USER_ID,
        action="create_ticket",
        payload={"title": "x"},
    )
    approvals.decide(approval.id, user_id=USER_ID, approved=True)
    executor.reports_failure = True
    writer = WriteExecutor(executor=executor, approvals=approvals)

    await writer.execute(approval.id, user_id=USER_ID)

    with pytest.raises(ApprovalTransitionError):
        await writer.execute(approval.id, user_id=USER_ID)
