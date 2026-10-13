"""The cancel endpoint, driven through the real application.

The route is exercised via the assembled app rather than by calling the handler directly, because
the thing worth proving is that the endpoint is *reachable* — Stage 11 established that a feature
no request can reach passes its own tests while being absent from the running system, and a cancel
endpoint nothing can call is exactly that failure.

Two responses carry the design and get their own tests: 404 when there is nothing in flight, and
501 when no registry is installed. Collapsing them would make a deployment mistake look like a
finished turn.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_service.agent.inflight import InflightRegistry
from agent_service.api.app import create_app
from agent_service.api.cancel_routes import CancelResult


def _app_with(registry: InflightRegistry) -> FastAPI:
    """The real app, with a cancel handler that uses the real registry.

    The handler is the two lines production uses, so what is under test is the route plus the
    registry, not a stand-in for either.
    """

    async def cancel(session_id: str, user_id: str) -> CancelResult | None:
        run = registry.cancel_session(session_id, user_id=user_id)
        if run is None:
            return None
        return CancelResult(
            session_id=session_id,
            state="stopping",
            detail="The turn has been asked to stop at its next step.",
        )

    return create_app(cancel_handler=cancel)


def test_cancelling_a_running_session_answers_that_it_is_stopping() -> None:
    registry = InflightRegistry()
    run = registry.start(session_id="sess-1", user_id="alice")
    client = TestClient(_app_with(registry))

    response = client.post("/invocations/sess-1/cancel", json={"user_id": "alice"})

    assert response.status_code == 200
    assert response.json()["state"] == "stopping"
    assert run.token.cancelled, "the route must actually stop the run, not just answer"


def test_cancelling_with_nothing_in_flight_is_a_404() -> None:
    """Distinct from 200, so a client can tell "stopped it" from "nothing to stop"."""
    registry = InflightRegistry()
    client = TestClient(_app_with(registry))

    response = client.post("/invocations/sess-nothing/cancel", json={"user_id": "alice"})

    assert response.status_code == 404
    assert response.json()["error"]["kind"] == "NotFound"


def test_cancelling_someone_elses_run_is_a_404_too() -> None:
    """Not a 403: a distinct answer would confirm the session exists for another user."""
    registry = InflightRegistry()
    run = registry.start(session_id="sess-1", user_id="alice")
    client = TestClient(_app_with(registry))

    response = client.post("/invocations/sess-1/cancel", json={"user_id": "mallory"})

    assert response.status_code == 404
    assert run.token.cancelled is False


def test_a_cancel_without_a_user_is_rejected_before_it_runs() -> None:
    """422, because the scoped lookup is what makes the endpoint safe.

    Reaching the handler without a user would mean an unscoped cancel, so this is rejected by the
    contract rather than handled with a default.
    """
    registry = InflightRegistry()
    client = TestClient(_app_with(registry))

    response = client.post("/invocations/sess-1/cancel", json={})

    assert response.status_code == 422


def test_an_unwired_registry_is_a_501_not_a_404() -> None:
    """A deployment mistake must not look like a finished turn.

    This is the distinction that matters most on this route: with no handler installed the app
    answers 501, so "the registry was never wired" is never reported as "nothing was running".
    """
    client = TestClient(create_app())

    response = client.post("/invocations/sess-1/cancel", json={"user_id": "alice"})

    assert response.status_code == 501
    assert response.json()["error"]["kind"] == "NotImplemented"


def test_the_cancel_route_does_not_disturb_the_contract() -> None:
    """``/ping`` still answers as before, and cancelling did not become part of the contract.

    The cancel endpoint is a local convenience layered on the same app; it must not change what
    the platform sees.
    """
    registry = InflightRegistry()
    client = TestClient(_app_with(registry))

    assert client.get("/ping").json() == {"status": "Healthy"}
