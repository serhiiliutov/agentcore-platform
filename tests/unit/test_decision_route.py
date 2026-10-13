"""The approval decision route and the gate adapter behind it.

The gate itself is proven in `test_approval_gate.py`. What is tested here is the *seam*: that
the route maps the adapter's outcomes onto the right status codes, that the adapter's one
branch — declining does not execute — is real, and that the two error kinds stay distinct.

Hermetic: in-process HTTP over an in-memory database, with a spy transport. No server process.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import agent_service.db.models  # noqa: F401 - registers every table on Base.metadata
from agent_service.agent.execution import WriteExecutor
from agent_service.agent.gate import ApprovalGate
from agent_service.api.app import create_app
from agent_service.api.approval_routes import (
    ApprovalDecisionResult,
    DecisionConflictError,
    create_approval_router,
)
from agent_service.config import Settings
from agent_service.db.base import Base, create_session_factory, register_sqlite_pragmas
from agent_service.db.models.approval import ApprovalState
from agent_service.db.models.conversation import ChatSession
from agent_service.db.repositories.approvals import ApprovalRepository
from tests.unit.test_approval_gate import SpyExecutor

SESSION_ID = "session-1"
USER_ID = "user-1"
OTHER_USER = "user-2"

DECIDE_URL = "/invocations/approvals/{}/decide"
NOT_WIRED_URL = "/invocations/approvals/any-id/decide"


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
def approvals(session: Session) -> ApprovalRepository:
    return ApprovalRepository(session)


@pytest.fixture
def executor() -> SpyExecutor:
    return SpyExecutor()


def _proposal(approvals: ApprovalRepository, **overrides: Any) -> Any:
    """A pending proposal with everything needed to execute it."""
    fields: dict[str, Any] = {
        "session_id": SESSION_ID,
        "user_id": USER_ID,
        "action": "create_ticket",
        "payload": {"title": "about the printer"},
        "client_request_id": "key-1",
    }
    fields.update(overrides)
    return approvals.propose(**fields)


# --- the route, unwired -------------------------------------------------------


def test_the_route_exists_even_with_no_handler() -> None:
    """A 404 would look like a typo; the route is real and says it is not wired."""
    client = TestClient(create_app(Settings(database_url="sqlite+pysqlite:///:memory:")))

    response = client.post(NOT_WIRED_URL, json={"user_id": USER_ID, "approved": True})

    assert response.status_code == 501
    assert response.json()["error"]["kind"] == "NotImplemented"


def test_a_decision_without_a_user_is_rejected() -> None:
    """The same required field as /invocations, for the same reason: scoped reads."""
    client = TestClient(create_app(Settings(database_url="sqlite+pysqlite:///:memory:")))

    response = client.post(NOT_WIRED_URL, json={"approved": True})

    assert response.status_code == 422


def test_a_decision_without_a_verdict_is_rejected() -> None:
    client = TestClient(create_app(Settings(database_url="sqlite+pysqlite:///:memory:")))

    response = client.post(NOT_WIRED_URL, json={"user_id": USER_ID})

    assert response.status_code == 422


# --- the route, wired, against a scripted handler ----------------------------


def _client(handler: Any) -> TestClient:
    settings = Settings(database_url="sqlite+pysqlite:///:memory:")
    return TestClient(create_app(settings, decision_handler=handler))


def test_an_approval_is_reported_as_executed() -> None:
    async def handler(_id: str, _user: str, approved: bool) -> ApprovalDecisionResult:
        assert approved is True
        return ApprovalDecisionResult(
            approval_id="a1",
            action="create_ticket",
            state=ApprovalState.EXECUTED.value,
            outcome="executed",
            detail="ticket TCK-9 created",
        )

    response = _client(handler).post(
        DECIDE_URL.format("a1"), json={"user_id": USER_ID, "approved": True}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "executed"
    assert body["state"] == "executed"
    assert body["replayed"] is False


def test_a_conflict_is_a_409_rather_than_a_400() -> None:
    """The request is well-formed; the state is what prevents it."""

    async def handler(_id: str, _user: str, _approved: bool) -> ApprovalDecisionResult:
        raise DecisionConflictError("approval 'a1' is 'declined' and cannot be decided again")

    response = _client(handler).post(
        DECIDE_URL.format("a1"), json={"user_id": USER_ID, "approved": True}
    )

    assert response.status_code == 409
    assert response.json()["error"]["kind"] == "Conflict"


def test_a_replay_is_reported_on_the_response() -> None:
    """So the caller does not claim a fresh ticket was created."""

    async def handler(_id: str, _user: str, _approved: bool) -> ApprovalDecisionResult:
        return ApprovalDecisionResult(
            approval_id="a1",
            action="create_ticket",
            state=ApprovalState.EXECUTED.value,
            outcome="executed",
            detail="the backend reported this was already done",
            replayed=True,
        )

    response = _client(handler).post(
        DECIDE_URL.format("a1"), json={"user_id": USER_ID, "approved": True}
    )

    assert response.json()["replayed"] is True


def test_the_router_can_be_built_without_an_app() -> None:
    """So a caller can mount it elsewhere, which is why it is a router and not a route."""
    router = create_approval_router()

    assert router.prefix == "/invocations/approvals"


# --- the adapter's own branch -------------------------------------------------


async def test_approving_executes(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    approval = _proposal(approvals)
    gate = ApprovalGate(
        executor=WriteExecutor(executor=executor, approvals=approvals), approvals=approvals
    )

    result = await gate.decide(approval.id, USER_ID, approved=True)

    assert result.outcome == "executed"
    assert result.state == ApprovalState.EXECUTED.value
    assert executor.names == ["create_ticket"]


async def test_declining_does_not_execute(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """The one rule the adapter decides, because it cannot live below it."""
    approval = _proposal(approvals)
    gate = ApprovalGate(
        executor=WriteExecutor(executor=executor, approvals=approvals), approvals=approvals
    )

    result = await gate.decide(approval.id, USER_ID, approved=False)

    assert result.outcome == "declined"
    assert result.state == ApprovalState.DECLINED.value
    assert executor.calls == [], "a declined proposal must not reach the transport"


async def test_declined_is_distinct_from_failed(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """A person saying no and a backend refusing are different things to report."""
    declined = _proposal(approvals, client_request_id="key-a")
    failing = _proposal(approvals, client_request_id="key-b")
    executor.raises = RuntimeError("the ticketing API is unreachable")
    gate = ApprovalGate(
        executor=WriteExecutor(executor=executor, approvals=approvals), approvals=approvals
    )

    said_no = await gate.decide(declined.id, USER_ID, approved=False)
    executor.raises = None
    executor.raises = RuntimeError("the ticketing API is unreachable")
    refused = await gate.decide(failing.id, USER_ID, approved=True)

    assert said_no.outcome == "declined"
    assert said_no.state == ApprovalState.DECLINED.value
    assert refused.outcome == "failed"
    assert refused.state == ApprovalState.FAILED.value
    assert "refused it" in refused.detail


async def test_a_second_decision_is_a_conflict(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """Decline once, and approving afterwards cannot resurrect it."""
    approval = _proposal(approvals)
    gate = ApprovalGate(
        executor=WriteExecutor(executor=executor, approvals=approvals), approvals=approvals
    )
    await gate.decide(approval.id, USER_ID, approved=False)

    with pytest.raises(DecisionConflictError):
        await gate.decide(approval.id, USER_ID, approved=True)

    assert executor.calls == []


async def test_another_users_decision_is_a_conflict(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    approval = _proposal(approvals)
    gate = ApprovalGate(
        executor=WriteExecutor(executor=executor, approvals=approvals), approvals=approvals
    )

    with pytest.raises(DecisionConflictError):
        await gate.decide(approval.id, OTHER_USER, approved=True)

    assert executor.calls == []


async def test_execution_sends_the_stored_payload_through_the_gate(
    session: Session, approvals: ApprovalRepository, executor: SpyExecutor
) -> None:
    """The end-to-end path: approve, and the row's arguments are what the tool receives."""
    approval = _proposal(
        approvals, payload={"title": "the exact title"}, client_request_id="row-key"
    )
    gate = ApprovalGate(
        executor=WriteExecutor(executor=executor, approvals=approvals), approvals=approvals
    )

    await gate.decide(approval.id, USER_ID, approved=True)

    _name, sent = executor.calls[0]
    assert sent["title"] == "the exact title"
    assert sent["client_request_id"] == "row-key"
