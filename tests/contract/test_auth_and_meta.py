"""The mock's authentication and meta endpoints.

Two behaviours here drive design decisions later in the project:

* ``/health`` is **not** protected. The MCP server and the compose health check can
  therefore probe the mock without holding a credential, while every ticket route
  still refuses an unauthenticated caller.
* A rejected request is a ``401`` with an ``{"error", "detail"}`` body. That body
  shape is *not* the same as the mock's other failures — FastAPI's own ``404`` and
  ``422`` use ``{"detail": ...}`` alone — which is exactly why the error taxonomy
  in Stage 4 has to handle both rather than assuming one.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import MockContract


def test_health_is_reachable_without_a_credential(client: TestClient) -> None:
    """The health route must not require a key, or the stack cannot self-check."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_reports_the_seeded_inventory(
    client: TestClient, mock: MockContract
) -> None:
    """Health doubles as a cheap way to confirm the seed is loaded."""
    body = client.get("/health").json()

    assert body["tickets"] == mock.seed_ticket_count
    assert body["flaky"] is False


def test_search_without_a_key_is_unauthorized(client: TestClient) -> None:
    response = client.get("/tickets")

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_search_with_a_wrong_key_is_unauthorized(
    client: TestClient, mock: MockContract
) -> None:
    response = client.get("/tickets", headers=mock.auth_headers("not-the-key"))

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_search_with_the_right_key_succeeds(
    client: TestClient, mock: MockContract
) -> None:
    response = client.get("/tickets", headers=mock.auth_headers())

    assert response.status_code == 200


def test_the_401_body_says_which_header_is_missing(client: TestClient) -> None:
    """A model reading this error should not have to guess what went wrong."""
    body = client.get("/tickets").json()

    assert body["error"] == "unauthorized"
    assert "X-API-Key" in body["detail"]


def test_the_header_name_is_case_insensitive(
    client: TestClient, mock: MockContract
) -> None:
    """HTTP header names are case-insensitive; clients must not be punished."""
    response = client.get("/tickets", headers={"x-api-key": mock.api_key})

    assert response.status_code == 200


def test_admin_reset_is_protected(client: TestClient) -> None:
    """Reset mutates state, so it must not be reachable unauthenticated."""
    assert client.post("/admin/reset").status_code == 401


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    """The mock stamps a correlation id, even on a rejection.

    Worth pinning: it is the only identifier the mock will echo back, so it is what
    the MCP server can log to correlate its own attempts with the mock's view.
    """
    for path in ("/health", "/tickets"):
        response = client.get(path)
        assert response.headers.get("X-Request-Id"), f"no X-Request-Id on {path}"


def test_the_openapi_document_is_public(client: TestClient) -> None:
    """The schema is documentation, not data, and stays reachable without a key."""
    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Mock IT Ticketing API"
