"""Contract tests for the agent service's AgentCore Runtime surface.

Hermetic: the app is built in-process with an injected handler, so no database, no MCP
server and no model provider are involved. What is under test is the *contract* — the two
routes, their status codes, and the fact that a bad request never reaches orchestration.

Each test pins something a caller depends on:

* ``/ping`` answers exactly ``Healthy`` or ``HealthyBusy``, and reports busy while work is
  in flight, because a supervisor uses that to decide whether to keep routing.
* ``/invocations`` files a body without ``user_id`` under one documented scope rather than
  rejecting it, because `CONTRACT.md`'s own smoke test sends no user id — and every user-scoped
  read downstream still has an identity to be scoped by.
* A turn that ends without an installed loop answers 501, not 500 — "not wired" and "broken"
  are different claims and a monitor should not confuse them.
* The runtime's session header wins over the body field, because a platform that sets a
  header means it.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from agent_service.api.app import SESSION_HEADER, create_app
from agent_service.api.models import DEFAULT_USER_ID, InvocationRequest, InvocationResponse
from agent_service.config import Settings

#: Any database URL will do: nothing in these tests opens one. A full SQLAlchemy URL is
#: required because Settings validates the scheme at startup.
_TEST_DATABASE_URL = "sqlite+pysqlite:///:memory:"


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url=_TEST_DATABASE_URL)


def _client(app: FastAPI) -> AsyncClient:
    """An HTTP client bound to the app in-process, with no socket involved."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://agent.test")


async def _post(
    app: FastAPI, body: dict[str, object], headers: dict[str, str] | None = None
) -> Response:
    async with _client(app) as client:
        return await client.post("/invocations", json=body, headers=headers or {})


# --- /ping -------------------------------------------------------------------


async def test_ping_reports_healthy_when_idle(settings: Settings) -> None:
    app = create_app(settings)

    async with _client(app) as client:
        response = await client.get("/ping")

    assert response.status_code == 200
    assert response.json() == {"status": "Healthy"}


async def test_ping_reports_busy_while_an_invocation_is_in_flight(settings: Settings) -> None:
    """The supervisor's signal, so it must reflect real work rather than a flag.

    The handler blocks on an event the test controls, so /ping is queried at a moment when
    the first request is provably still running.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_handler(request: InvocationRequest) -> InvocationResponse:
        started.set()
        await release.wait()
        return InvocationResponse(session_id=request.session_id or "s-1")

    app = create_app(settings, invocation_handler=blocking_handler)

    async with _client(app) as client:
        invocation = asyncio.create_task(
            client.post(
                "/invocations",
                json={"prompt": "hello", "user_id": "u-1"},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)

        busy = await client.get("/ping")
        assert busy.json() == {"status": "HealthyBusy"}

        release.set()
        await asyncio.wait_for(invocation, timeout=5)

        # And it goes back to idle once the turn finishes: a counter that only ever
        # incremented would leave the service permanently "busy".
        idle = await client.get("/ping")
        assert idle.json() == {"status": "Healthy"}


# --- /invocations: request validation ----------------------------------------


async def test_invocations_defaults_an_absent_user_id(settings: Settings) -> None:
    """The contract's own smoke-test body carries no user id, and must still be answerable.

    The scope is not "nothing": it is `DEFAULT_USER_ID`, a real identity for every user-scoped read
    downstream — one shared bucket, which is the honest alternative to a 422. The reason it is a
    default at all is concrete: `CONTRACT.md` documents ``{"prompt": ...}`` as the smoke test, and
    a service whose smoke test fails validation reads as broken before it reads as careful.
    """
    seen: list[str] = []

    async def handler(request: InvocationRequest) -> InvocationResponse:
        seen.append(request.user_id)
        return InvocationResponse(session_id=request.session_id or "s-1")

    app = create_app(settings, invocation_handler=handler)

    response = await _post(app, {"prompt": "where is my ticket?"})

    assert response.status_code == 200
    assert seen == [DEFAULT_USER_ID]


async def test_invocations_uses_the_user_id_it_is_given(settings: Settings) -> None:
    """A supplied id is passed through untouched; the default only fills a gap."""
    seen: list[str] = []

    async def handler(request: InvocationRequest) -> InvocationResponse:
        seen.append(request.user_id)
        return InvocationResponse(session_id=request.session_id or "s-1")

    app = create_app(settings, invocation_handler=handler)

    await _post(app, {"prompt": "hi", "user_id": "alice"})

    assert seen == ["alice"]


async def test_invocations_rejects_an_empty_user_id(settings: Settings) -> None:
    """An empty string is a client bug, not an omission — so it is refused rather than scoped."""
    app = create_app(settings)

    response = await _post(app, {"prompt": "hi", "user_id": ""})

    assert response.status_code == 422


async def test_invocations_rejects_an_empty_prompt(settings: Settings) -> None:
    app = create_app(settings)

    response = await _post(app, {"prompt": "", "user_id": "u-1"})

    assert response.status_code == 422


async def test_invocations_ignores_unknown_fields(settings: Settings) -> None:
    """Forward compatibility: a platform sending a newer field must not break a turn."""
    app = create_app(
        settings,
        invocation_handler=lambda request: _echo(request),
    )

    response = await _post(
        app,
        {"prompt": "hi", "user_id": "u-1", "some_future_field": 42},
    )

    assert response.status_code == 200


# --- /invocations: the installed handler --------------------------------------


async def _echo(request: InvocationRequest) -> InvocationResponse:
    return InvocationResponse(
        output=f"echo: {request.prompt}",
        session_id=request.session_id or "new-session",
    )


async def test_invocations_returns_the_handlers_response(settings: Settings) -> None:
    app = create_app(settings, invocation_handler=_echo)

    response = await _post(app, {"prompt": "hello", "user_id": "u-1"})

    assert response.status_code == 200
    body = response.json()
    assert body["output"] == "echo: hello"
    assert body["stop_reason"] == "completed"
    assert body["pending_approval"] is None


async def test_the_session_header_wins_over_the_body_field(settings: Settings) -> None:
    """A platform that sets the header means it; a body field must not override it."""
    app = create_app(settings, invocation_handler=_echo)

    response = await _post(
        app,
        {"prompt": "hi", "user_id": "u-1", "session_id": "from-body"},
        headers={SESSION_HEADER: "from-header"},
    )

    assert response.json()["session_id"] == "from-header"


async def test_the_body_session_id_is_used_when_no_header_is_present(
    settings: Settings,
) -> None:
    app = create_app(settings, invocation_handler=_echo)

    response = await _post(app, {"prompt": "hi", "user_id": "u-1", "session_id": "from-body"})

    assert response.json()["session_id"] == "from-body"


# --- /invocations: the loop is not installed ----------------------------------


async def test_an_uninstalled_loop_answers_501_rather_than_500(settings: Settings) -> None:
    """'Not wired yet' and 'broken' are different claims.

    A 500 would send an operator looking for a bug in code that is deliberately absent, and
    would make a monitor page for a service that is behaving exactly as built.
    """
    app = create_app(settings)

    response = await _post(app, {"prompt": "hello", "user_id": "u-1"})

    assert response.status_code == 501
    assert response.json()["error"]["kind"] == "NotImplemented"


async def test_the_501_body_says_what_is_missing(settings: Settings) -> None:
    """An operator reading only the response should learn what to install."""
    app = create_app(settings)

    response = await _post(app, {"prompt": "hello", "user_id": "u-1"})

    assert "invocation handler" in response.json()["error"]["detail"]


# --- The response envelope ----------------------------------------------------


async def test_a_response_carries_every_contract_field(settings: Settings) -> None:
    """The runtime may read any of these; omitting one would be a silent break."""
    app = create_app(settings, invocation_handler=_echo)

    response = await _post(app, {"prompt": "hi", "user_id": "u-1"})

    assert set(response.json()) >= {
        "output",
        "tool_calls",
        "pending_approval",
        "session_id",
        "stop_reason",
        "steps",
    }


async def test_tool_calls_default_to_an_empty_list_not_null(settings: Settings) -> None:
    """A client iterating tool_calls must not have to guard against null."""
    app = create_app(settings, invocation_handler=_echo)

    response = await _post(app, {"prompt": "hi", "user_id": "u-1"})

    assert response.json()["tool_calls"] == []
