"""The client routes, and the absence of a page.

Two things are pinned here, and the first is unusual enough to explain.

**This service serves no HTML.** It used to serve a small hand-written page from ``GET /`` as well
as the endpoints below, so there were two front ends for one API and two transcripts to keep
correct. The page was removed and the front end is now ``services/chat/`` — a Chainlit app that is a
client of exactly this surface. The tests below assert both halves of that: no route renders a page,
and the endpoints the chat depends on are wired and behave.

The absence is asserted rather than assumed because it is a *decision*, and a decision that nothing
tests is a decision the next change can undo quietly. A reviewer reading `client_routes.py` sees the
reasoning; a route reappearing at ``/`` would fail here, which is the part that survives a rewrite.

**The transcript read takes two identifiers.** It is the most sensitive read in the service — a
whole conversation — so the test asserts the user id reaches the reader rather than only the session
id. A route that scoped by session alone would serve any conversation to anyone who guessed an id,
and a session id is not a secret: the client displays it and it appears in logs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_service.api.app import create_app
from agent_service.api.client_routes import (
    SessionSummary,
    TranscriptMessage,
)
from agent_service.api.models import PendingApproval
from agent_service.config import Settings

EXPIRY = datetime.now(UTC) + timedelta(minutes=30)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"database_url": "sqlite+pysqlite:///:memory:"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _approval() -> PendingApproval:
    """One proposal, with the arguments a real one carries."""
    return PendingApproval(
        approval_id="approval-1",
        tool_name="create_ticket",
        arguments={"title": "the printer is jammed", "client_request_id": "key-1"},
        summary="Create one ticket.",
        expires_at=EXPIRY,
    )


def _app(
    *,
    pending: Callable[[str], Awaitable[Sequence[PendingApproval]]] | None = None,
    sessions: Callable[[str], Awaitable[Sequence[SessionSummary]]] | None = None,
    transcript: Callable[[str, str], Awaitable[Sequence[TranscriptMessage]]] | None = None,
) -> FastAPI:
    return create_app(
        _settings(),
        pending_approvals=pending,
        session_listing=sessions,
        transcript_reader=transcript,
    )


async def _one_approval(_user_id: str) -> Sequence[PendingApproval]:
    return [_approval()]


async def _no_sessions(_user_id: str) -> Sequence[SessionSummary]:
    return []


async def _a_transcript(_session_id: str, _user_id: str) -> Sequence[TranscriptMessage]:
    return [TranscriptMessage(sequence=1, role="user", content="hello")]


# --- the page is gone --------------------------------------------------------


def test_this_service_serves_no_page() -> None:
    """``GET /`` is a 404, and the body is not HTML.

    Both halves matter. A 404 alone would also be produced by a page route that had been accidentally
    renamed, and the point is that there is no front end here at all — so the content type is checked
    too, which is the part that fails if somebody mounts a static directory or a template instead of a
    route.
    """
    response = TestClient(_app()).get("/")

    assert response.status_code == 404
    assert "text/html" not in response.headers.get("content-type", "")


def test_no_route_in_the_application_serves_the_root_path() -> None:
    """The stronger form of the same claim, against the assembled route table.

    Asserted over the routes rather than only over a request, because a route that exists but is
    shadowed — or served only for one method — would answer 405 rather than 404 and read as a bug
    rather than as a decision.
    """
    app = _app()

    roots = [route for route in app.routes if getattr(route, "path", None) == "/"]

    assert roots == [], "the service gained a page route; the front end is services/chat/"


def test_the_contract_routes_are_still_there() -> None:
    """Removing the page must not have removed the contract.

    The regression this guards is a careless deletion: `/` and `/invocations` are one edit apart in
    the file, and the health route is what the container's healthcheck and the eval's readiness wait
    both depend on.
    """
    paths = {getattr(route, "path", None) for route in _app().routes}

    assert {"/ping", "/invocations"} <= paths


# --- the routes a chat client needs -----------------------------------------


def test_the_approval_listing_returns_the_proposals_it_is_given() -> None:
    """The listing that makes a durable proposal visible after a restart.

    This is the endpoint the whole restart requirement is worthless without: the chat fetches it on
    load, so an approval proposed before the service restarted is still on screen afterwards.
    """
    response = TestClient(_app(pending=_one_approval)).get("/api/approvals?user_id=alice")

    assert response.status_code == 200
    approvals = response.json()["approvals"]
    assert [item["approval_id"] for item in approvals] == ["approval-1"]
    assert approvals[0]["tool_name"] == "create_ticket"
    assert approvals[0]["arguments"]["client_request_id"] == "key-1"


def test_an_unwired_approval_listing_answers_501_not_500() -> None:
    """"Not configured" stays distinguishable from "broken", as on the other endpoints."""
    response = TestClient(_app()).get("/api/approvals?user_id=alice")

    assert response.status_code == 501
    assert response.json()["error"]["kind"] == "NotImplemented"


def test_a_user_scoped_read_refuses_to_answer_without_a_user() -> None:
    """No default user on these reads, deliberately.

    Every other user-scoped endpoint defaults `user_id` to one shared bucket, which is the honest
    answer for a turn. It is the wrong answer here: a listing with no user would show one person
    another person's proposals, so the field is required and a request without it is refused before
    any handler runs.
    """
    client = TestClient(_app(pending=_one_approval))

    assert client.get("/api/approvals").status_code == 422
    assert client.get("/api/sessions").status_code == 422


def test_the_session_listing_is_reachable() -> None:
    """The menu a client resumes a conversation from."""
    response = TestClient(_app(sessions=_no_sessions)).get("/api/sessions?user_id=alice")

    assert response.status_code == 200
    assert response.json()["sessions"] == []


def test_a_transcript_read_receives_both_the_session_and_the_user() -> None:
    """The owner check cannot be the route's job, so the reader must be given the user.

    `client_routes.py` documents this as deliberate: the reader is the layer that can scope the
    query, and a route that passed only the session id would leave every future call site able to
    forget the ownership check. Asserted on the arguments the reader actually receives.
    """
    seen: list[tuple[str, str]] = []

    async def transcript(session_id: str, user_id: str) -> Sequence[TranscriptMessage]:
        seen.append((session_id, user_id))
        return await _a_transcript(session_id, user_id)

    response = TestClient(_app(transcript=transcript)).get(
        "/api/sessions/sess-1/messages?user_id=bob"
    )

    assert response.status_code == 200
    assert seen == [("sess-1", "bob")]
    body = response.json()
    # The response says which conversation it is showing, which a bare list could not.
    assert body["session_id"] == "sess-1"
    assert [message["content"] for message in body["messages"]] == ["hello"]


def test_an_unwired_transcript_reader_answers_501() -> None:
    response = TestClient(_app()).get("/api/sessions/sess-1/messages?user_id=alice")

    assert response.status_code == 501
    assert response.json()["error"]["kind"] == "NotImplemented"
