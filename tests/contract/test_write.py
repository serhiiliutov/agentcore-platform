"""Writing: create, comment, update, and the admin reset.

The single most important fact pinned here is the last one:
**the mock has no idempotency of its own.** Post the same ticket twice and you get
two tickets. Nothing in the mock deduplicates, and nothing it returns lets you tell
a retry from a genuine second request.

That is why the MCP server owns an idempotency ledger keyed by a client request id
(Stage 4). Without it, the only safe response to a lost write response would be to
give up — and a model that gives up after a timeout leaves the user with no ticket
and no explanation. This test is the evidence for that design decision.

The other facts pinned here are the ones the approval gate depends on: a create
returns ``201`` with the new id and starts ``open``, the id counter is monotonic, and
validation failures are ``422`` with FastAPI's ``{"detail"}`` body rather than the
``{"error", "detail"}`` shape of the mock's own middleware errors.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import JsonObject, MockContract

#: A minimal valid create body. Each test overrides only what it is testing.
_VALID_TICKET: dict[str, str] = {
    "title": "Laptop will not boot",
    "description": "Pressed power, fan spins, no display.",
    "priority": "high",
    "category": "hardware",
    "requester": "Priya Raman",
}


def _create(
    client: TestClient,
    mock: MockContract,
    **overrides: str,
) -> JsonObject:
    """Create a ticket, asserting the mock accepted it."""
    response = client.post(
        "/tickets",
        headers=mock.auth_headers(),
        json={**_VALID_TICKET, **overrides},
    )
    assert response.status_code == 201, response.text
    # Annotated explicitly: `response.json()` is `Any`, and returning it directly
    # would smuggle untyped data through a typed signature.
    body: JsonObject = response.json()
    return body


def _count(client: TestClient, mock: MockContract) -> int:
    """The current number of tickets, from the search endpoint's total."""
    response = client.get("/tickets", headers=mock.auth_headers(), params={"page_size": 1})
    assert response.status_code == 200
    total = response.json()["total"]
    assert isinstance(total, int)
    return total


def test_creating_a_ticket_returns_201_and_the_new_record(
    client: TestClient, mock: MockContract
) -> None:
    created = _create(client, mock)

    assert created["id"] == mock.first_created_ticket_id
    assert created["title"] == _VALID_TICKET["title"]
    assert created["requester"] == _VALID_TICKET["requester"]


def test_a_new_ticket_starts_open_unassigned_and_undiscussed(
    client: TestClient, mock: MockContract
) -> None:
    """These three defaults are what the agent reports back to the user."""
    created = _create(client, mock)

    assert created["status"] == "open"
    assert created["assignee"] is None
    assert created["comments"] == []


def test_a_new_ticket_gets_a_created_and_updated_timestamp(
    client: TestClient, mock: MockContract
) -> None:
    created = _create(client, mock)

    assert created["created_at"] == created["updated_at"]
    assert str(created["created_at"]).endswith("Z")


def test_ticket_ids_increment_monotonically(client: TestClient, mock: MockContract) -> None:
    """Two creates produce two distinct ids, in order."""
    first = _create(client, mock)
    second = _create(client, mock, title="Second ticket")

    assert first["id"] == mock.first_created_ticket_id
    assert second["id"] == "TCK-1026"


def test_a_created_ticket_is_immediately_readable(client: TestClient, mock: MockContract) -> None:
    """The create response is not a promise: the ticket is really there."""
    created = _create(client, mock)

    fetched = client.get(f"/tickets/{created['id']}", headers=mock.auth_headers())

    assert fetched.status_code == 200
    assert fetched.json()["title"] == _VALID_TICKET["title"]


def test_a_created_ticket_appears_in_search(client: TestClient, mock: MockContract) -> None:
    before = _count(client, mock)
    _create(client, mock)

    assert _count(client, mock) == before + 1


def test_the_same_payload_creates_two_distinct_tickets(
    client: TestClient, mock: MockContract
) -> None:
    """The mock does not deduplicate — which is exactly why the MCP server must.

    A client that retried a timed-out create against this API would produce a
    duplicate ticket. The ledger is the only thing that prevents that, and this test
    is the evidence that it is needed rather than assumed.
    """
    first = _create(client, mock)
    second = _create(client, mock)

    assert first["id"] != second["id"]
    assert first["title"] == second["title"]


def test_a_too_short_title_is_rejected(client: TestClient, mock: MockContract) -> None:
    response = client.post(
        "/tickets", headers=mock.auth_headers(), json={**_VALID_TICKET, "title": "ab"}
    )

    assert response.status_code == 422
    assert "detail" in response.json()


def test_a_missing_requester_is_rejected_without_touching_state(
    client: TestClient, mock: MockContract
) -> None:
    """Validation runs before the handler, so nothing is written."""
    before = _count(client, mock)

    response = client.post(
        "/tickets", headers=mock.auth_headers(), json={"title": "No requester here"}
    )

    assert response.status_code == 422
    assert _count(client, mock) == before


def test_an_unknown_priority_is_rejected(client: TestClient, mock: MockContract) -> None:
    response = client.post(
        "/tickets",
        headers=mock.auth_headers(),
        json={**_VALID_TICKET, "priority": "whenever"},
    )

    assert response.status_code == 422


def test_commenting_appends_to_the_ticket(client: TestClient, mock: MockContract) -> None:
    response = client.post(
        f"/tickets/{mock.ticket_with_comments}/comments",
        headers=mock.auth_headers(),
        json={"author": "Aisha Khan", "body": "Still cannot reach the drive."},
    )

    assert response.status_code == 201
    comment = response.json()
    assert comment["author"] == "Aisha Khan"
    assert comment["id"] == "CMT-0001"

    ticket = client.get(f"/tickets/{mock.ticket_with_comments}", headers=mock.auth_headers()).json()
    assert [c["body"] for c in ticket["comments"]][-1] == "Still cannot reach the drive."


def test_commenting_touches_the_ticket_timestamp(client: TestClient, mock: MockContract) -> None:
    """So a comment resurfaces the ticket in the updated-at ordering."""
    before = client.get(
        f"/tickets/{mock.ticket_with_comments}", headers=mock.auth_headers()
    ).json()["updated_at"]

    client.post(
        f"/tickets/{mock.ticket_with_comments}/comments",
        headers=mock.auth_headers(),
        json={"author": "Aisha Khan", "body": "Any update?"},
    )

    after = client.get(f"/tickets/{mock.ticket_with_comments}", headers=mock.auth_headers()).json()[
        "updated_at"
    ]
    assert after >= before


def test_commenting_uses_an_uppercased_ticket_id(client: TestClient, mock: MockContract) -> None:
    response = client.post(
        f"/tickets/{mock.ticket_with_comments.lower()}/comments",
        headers=mock.auth_headers(),
        json={"author": "Aisha Khan", "body": "Lower-case id."},
    )

    assert response.status_code == 201


def test_commenting_on_a_missing_ticket_is_a_404(client: TestClient, mock: MockContract) -> None:
    response = client.post(
        "/tickets/TCK-9999/comments",
        headers=mock.auth_headers(),
        json={"author": "Aisha Khan", "body": "Nowhere."},
    )

    assert response.status_code == 404
    assert "error" not in response.json()


def test_an_empty_comment_body_is_rejected(client: TestClient, mock: MockContract) -> None:
    response = client.post(
        f"/tickets/{mock.ticket_with_comments}/comments",
        headers=mock.auth_headers(),
        json={"author": "Aisha Khan", "body": ""},
    )

    assert response.status_code == 422


def test_updating_a_ticket_changes_only_the_fields_given(
    client: TestClient, mock: MockContract
) -> None:
    """``PATCH`` is partial: absent fields keep their value."""
    before = client.get(
        f"/tickets/{mock.ticket_without_comments}", headers=mock.auth_headers()
    ).json()

    response = client.patch(
        f"/tickets/{mock.ticket_without_comments}",
        headers=mock.auth_headers(),
        json={"status": "in_progress"},
    )

    assert response.status_code == 200
    after = response.json()
    assert after["status"] == "in_progress"
    assert after["title"] == before["title"]
    assert after["requester"] == before["requester"]


def test_updating_with_nothing_to_change_is_a_400(client: TestClient, mock: MockContract) -> None:
    """An empty update is the caller's mistake, and the mock says so."""
    response = client.patch(
        f"/tickets/{mock.ticket_without_comments}",
        headers=mock.auth_headers(),
        json={},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["detail"] == "nothing to update"
    assert "error" not in body


def test_updating_a_missing_ticket_is_a_404(client: TestClient, mock: MockContract) -> None:
    response = client.patch(
        "/tickets/TCK-9999", headers=mock.auth_headers(), json={"status": "closed"}
    )

    assert response.status_code == 404


def test_reset_restores_the_seed_inventory(client: TestClient, mock: MockContract) -> None:
    """The reset endpoint is what makes eval runs repeatable."""
    _create(client, mock)

    response = client.post("/admin/reset", headers=mock.auth_headers())

    assert response.status_code == 200
    assert response.json() == {"status": "reset", "tickets": mock.seed_ticket_count}
    assert _count(client, mock) == mock.seed_ticket_count


def test_reset_discards_tickets_created_since_the_seed(
    client: TestClient, mock: MockContract
) -> None:
    created = _create(client, mock)
    client.post("/admin/reset", headers=mock.auth_headers())

    assert client.get(f"/tickets/{created['id']}", headers=mock.auth_headers()).status_code == 404


def test_reset_also_resets_the_id_counter(client: TestClient, mock: MockContract) -> None:
    """Deterministic ids are what let an eval assert on a created ticket."""
    _create(client, mock)
    client.post("/admin/reset", headers=mock.auth_headers())

    assert _create(client, mock)["id"] == mock.first_created_ticket_id


def test_reset_discards_comments_added_since_the_seed(
    client: TestClient, mock: MockContract
) -> None:
    client.post(
        f"/tickets/{mock.ticket_without_comments}/comments",
        headers=mock.auth_headers(),
        json={"author": "Aisha Khan", "body": "Temporary."},
    )
    client.post("/admin/reset", headers=mock.auth_headers())

    ticket = client.get(
        f"/tickets/{mock.ticket_without_comments}", headers=mock.auth_headers()
    ).json()
    assert ticket["comments"] == []


def test_writing_without_a_key_is_unauthorized(client: TestClient) -> None:
    """Both write paths refuse an unauthenticated caller.

    The real gate is the agent's approval step; this is the second lock, and it is
    the one that makes the API key worth keeping inside the MCP process only.
    """
    assert client.post("/tickets", json=_VALID_TICKET).status_code == 401
    assert (
        client.post("/tickets/TCK-1001/comments", json={"author": "a", "body": "b"}).status_code
        == 401
    )
