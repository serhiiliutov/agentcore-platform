"""Reading one ticket — ``GET /tickets/{id}``.

This is the endpoint behind the agent's ``get_ticket`` tool, and three details
shape that tool's schema and the error taxonomy:

* **The detail view includes comments; the list view does not.** A model that wants
  to discuss a ticket's history must therefore fetch it first. The tool description
  has to say so, or the model will answer from a search result and invent a thread.
* **Ticket ids are case-insensitive on the way in** — the handler upper-cases the
  path parameter — so ``tck-1002`` and ``TCK-1002`` are the same ticket. A model
  that lower-cases an id it read out of prose must not get a 404.
* **A missing ticket is a 404 whose body is ``{"detail": ...}``**, with no ``error``
  key. That differs from the mock's 401 and its chaos 429/500, which both carry
  ``error``. The MCP error taxonomy must read both shapes rather than assume one.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import JsonObject, MockContract

#: Fields the detail view is expected to expose for a ticket.
_EXPECTED_TICKET_FIELDS = {
    "id",
    "title",
    "description",
    "status",
    "priority",
    "category",
    "requester",
    "assignee",
    "created_at",
    "updated_at",
    "comments",
}


def _get(client: TestClient, mock: MockContract, ticket_id: str) -> JsonObject:
    """Fetch one ticket, asserting it was found."""
    response = client.get(f"/tickets/{ticket_id}", headers=mock.auth_headers())
    assert response.status_code == 200, response.text
    # Annotated explicitly: `response.json()` is `Any`, and returning it directly
    # from a typed function is exactly the kind of implicit `Any` this codebase bans.
    body: JsonObject = response.json()
    return body


def test_a_ticket_is_returned_with_its_full_record(client: TestClient, mock: MockContract) -> None:
    ticket = _get(client, mock, mock.ticket_with_comments)

    assert ticket["id"] == mock.ticket_with_comments
    assert set(ticket) >= _EXPECTED_TICKET_FIELDS


def test_the_detail_view_includes_comments(client: TestClient, mock: MockContract) -> None:
    """The delta between the list view and the detail view is exactly this field."""
    ticket = _get(client, mock, mock.ticket_with_comments)

    assert isinstance(ticket["comments"], list)
    assert ticket["comments"], "TCK-1002 is seeded with a comment"


def test_comments_carry_an_author_and_a_body(client: TestClient, mock: MockContract) -> None:
    comment = _get(client, mock, mock.ticket_with_comments)["comments"][0]

    assert comment["author"] == "Dana Whitfield"
    assert comment["body"]
    assert comment["created_at"]
    assert comment["id"].startswith("CMT-")


def test_a_ticket_without_comments_returns_an_empty_list(
    client: TestClient, mock: MockContract
) -> None:
    """Absent discussion is an empty list, not a missing key."""
    ticket = _get(client, mock, mock.ticket_without_comments)

    assert ticket["comments"] == []


def test_the_ticket_id_is_case_insensitive(client: TestClient, mock: MockContract) -> None:
    """A model that lower-cases an id must still find the ticket."""
    lowercased = _get(client, mock, mock.ticket_with_comments.lower())

    assert lowercased["id"] == mock.ticket_with_comments


def test_a_missing_ticket_is_a_404_with_the_fastapi_body_shape(
    client: TestClient, mock: MockContract
) -> None:
    """The absence of an ``error`` key here is the point.

    The mock answers its own middleware errors with ``{"error", "detail"}`` but
    delegates 404s to FastAPI, which returns ``{"detail"}`` alone. Code that reads
    only ``error`` would report a missing ticket as an unexplained failure.
    """
    response = client.get("/tickets/TCK-9999", headers=mock.auth_headers())

    assert response.status_code == 404
    body = response.json()
    assert "detail" in body
    assert "error" not in body
    assert "TCK-9999" in body["detail"]


def test_reading_a_ticket_requires_a_key(client: TestClient) -> None:
    assert client.get("/tickets/TCK-1001").status_code == 401
