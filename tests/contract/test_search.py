"""Search, filtering and pagination — the mock's ``GET /tickets``.

This is the endpoint behind the agent's ``search_tickets`` tool, and the pagination
arithmetic here is the reason that tool must return ``total`` and ``has_more`` to
the model rather than just a list. A model that cannot tell "these are all the
tickets" from "these are the first ten of thirty" will confidently answer the wrong
question.

The filters also have a subtlety worth pinning: ``q`` searches title *and*
description case-insensitively, while ``requester`` is an exact (but
case-insensitive) match on the whole name. Those two behave differently and the
tool's schema has to describe them differently.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import JsonObject, MockContract


def _search(
    client: TestClient,
    mock: MockContract,
    **params: str | int,
) -> JsonObject:
    """Call ``GET /tickets`` with the key and return the parsed body."""
    response = client.get("/tickets", headers=mock.auth_headers(), params=params)
    assert response.status_code == 200, response.text
    # Annotated explicitly: `response.json()` is `Any`, and a bare `return` of it
    # would smuggle untyped data through a typed signature.
    body: JsonObject = response.json()
    return body


def test_the_default_page_is_ten_items(client: TestClient, mock: MockContract) -> None:
    """The default page size comes from the mock, not from us."""
    body = _search(client, mock)
    items = body["items"]

    assert isinstance(items, list)
    assert len(items) == 10


def test_the_total_counts_every_match_not_just_the_page(
    client: TestClient, mock: MockContract
) -> None:
    """``total`` is the whole result set; it is what makes an answer honest."""
    body = _search(client, mock)

    assert body["total"] == mock.seed_ticket_count
    assert body["page"] == 1
    assert body["page_size"] == 10


def test_has_more_is_true_while_pages_remain(client: TestClient, mock: MockContract) -> None:
    assert _search(client, mock, page=1)["has_more"] is True
    assert _search(client, mock, page=2)["has_more"] is True


def test_the_last_page_reports_no_more_results(client: TestClient, mock: MockContract) -> None:
    """24 seeded tickets at 10 per page leaves 4 on the final page."""
    body = _search(client, mock, page=3)

    assert len(body["items"]) == 4
    assert body["has_more"] is False


def test_the_last_page_returns_a_partial_page_not_an_empty_one(
    client: TestClient, mock: MockContract
) -> None:
    """Past the end is empty, which is different from the last page."""
    assert _search(client, mock, page=4)["items"] == []
    assert _search(client, mock, page=4)["has_more"] is False


def test_search_items_omit_comments(client: TestClient, mock: MockContract) -> None:
    """The list view is deliberately lighter than the detail view.

    The tool contract depends on this: only ``get_ticket`` returns comments, so the
    model must fetch a ticket before it can talk about its discussion.
    """
    items = _search(client, mock)["items"]

    assert isinstance(items, list)
    for item in items:
        assert "comments" not in item
        assert "id" in item and "title" in item and "status" in item


def test_results_are_ordered_by_most_recently_updated(
    client: TestClient, mock: MockContract
) -> None:
    """Ordering is part of the contract: the newest activity leads."""
    items = _search(client, mock, page_size=50)["items"]

    assert isinstance(items, list)
    updated = [str(item["updated_at"]) for item in items]
    assert updated == sorted(updated, reverse=True)


def test_query_matches_the_title_case_insensitively(client: TestClient, mock: MockContract) -> None:
    body = _search(client, mock, q="vpn", page_size=50)
    items = body["items"]

    assert items, "expected at least one VPN ticket in the seed"
    for item in items:
        haystack = f"{item['title']} {item['description']}".lower()
        assert "vpn" in haystack, f"{item['id']} matched 'vpn' but contains neither field"


def test_query_also_matches_the_description(client: TestClient, mock: MockContract) -> None:
    """A phrase that appears only in a description still matches.

    "fan is loud" appears in TCK-1003's description and in no title, so a match here
    can only have come from the description field.
    """
    items = _search(client, mock, q="fan is loud", page_size=50)["items"]

    assert [item["id"] for item in items] == ["TCK-1003"]


def test_an_unmatched_query_is_an_empty_result_not_an_error(
    client: TestClient, mock: MockContract
) -> None:
    """A search that finds nothing is a successful search."""
    body = _search(client, mock, q="zzzz-no-such-ticket")

    assert body["items"] == []
    assert body["total"] == 0
    assert body["has_more"] is False


def test_status_filter_selects_only_that_status(client: TestClient, mock: MockContract) -> None:
    items = _search(client, mock, status="open", page_size=50)["items"]

    assert isinstance(items, list)
    assert items
    assert {item["status"] for item in items} == {"open"}


def test_priority_filter_selects_only_that_priority(client: TestClient, mock: MockContract) -> None:
    items = _search(client, mock, priority="urgent", page_size=50)["items"]

    assert isinstance(items, list)
    assert items
    assert {item["priority"] for item in items} == {"urgent"}


def test_requester_filter_is_an_exact_but_case_insensitive_match(
    client: TestClient, mock: MockContract
) -> None:
    """Unlike ``q``, this one does not do substring matching."""
    exact = _search(client, mock, requester="Priya Raman", page_size=50)
    lowercased = _search(client, mock, requester="priya raman", page_size=50)
    partial = _search(client, mock, requester="Priya", page_size=50)

    assert exact["total"] == lowercased["total"]
    assert exact["total"] > 0
    assert partial["total"] == 0, "requester is an exact name, not a prefix"


def test_category_filter_selects_only_that_category(client: TestClient, mock: MockContract) -> None:
    items = _search(client, mock, category="network", page_size=50)["items"]

    assert isinstance(items, list)
    assert items
    assert {item["category"] for item in items} == {"network"}


def test_filters_combine_with_and_semantics(client: TestClient, mock: MockContract) -> None:
    """Two filters narrow the set; they do not union it."""
    open_network = _search(client, mock, status="open", category="network", page_size=50)
    all_network = _search(client, mock, category="network", page_size=50)

    assert open_network["total"] <= all_network["total"]

    items = open_network["items"]
    assert isinstance(items, list)
    for item in items:
        assert item["status"] == "open"
        assert item["category"] == "network"


def test_page_size_is_capped_by_the_mock(client: TestClient, mock: MockContract) -> None:
    """The mock refuses an over-large page rather than silently truncating it.

    This matters for the tool schema: the server must clamp ``page_size`` itself,
    or a model asking for 100 gets a validation error instead of results.
    """
    response = client.get("/tickets", headers=mock.auth_headers(), params={"page_size": 51})

    assert response.status_code == 422


def test_an_invalid_page_number_is_rejected(client: TestClient, mock: MockContract) -> None:
    response = client.get("/tickets", headers=mock.auth_headers(), params={"page": 0})

    assert response.status_code == 422


def test_an_unknown_status_value_is_rejected(client: TestClient, mock: MockContract) -> None:
    """``status`` is a closed set, so a typo is a client error, not an empty result.

    The distinction is what lets the MCP server tell the model "you asked for
    something that does not exist" instead of "there are no such tickets".
    """
    response = client.get("/tickets", headers=mock.auth_headers(), params={"status": "in-progress"})

    assert response.status_code == 422
    assert "detail" in response.json()
