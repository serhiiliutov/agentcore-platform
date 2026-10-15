"""The read models must accept what the API actually sends.

Written for a defect whose shape is worth naming, because it is the kind that hides: the *read*
models pinned `category` to the closed set the mock **validates requests against**. The mock's own
seeded fixture contains a ticket with ``category: "infrastructure"``, which is outside that set — so
a page containing that ticket failed to parse, and since one model validates the whole page, **every
search broad enough to include TCK-1020** returned a tool error instead of results.

The message the agent saw was "the API returned a body that does not match SearchResult", which
points at the wrong layer: it read as a backend fault when the false constraint was ours. It was
found through a query that had been reduced to an empty string, which returns the unfiltered list —
a separate bug that merely made the blast radius visible.

The last test is the one that keeps the fix from being a loosening: what we *send* is still checked
against the closed set, because that is what the mock genuinely enforces.
"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from ticketing_mcp.models import Category, NewTicket, SearchQuery, SearchResult, TicketSummary

#: The value the seeded fixture really carries (TCK-1020), and the reason this file exists.
BACKEND_ONLY_CATEGORY = "infrastructure"


def _reported(**overrides: object) -> dict[str, object]:
    """One ticket as the mock reports it, with a category outside our request enum."""
    row: dict[str, object] = {
        "id": "TCK-1020",
        "title": "Network segmentation review",
        "description": "Review the proposed segmentation before the change window.",
        "status": "in_progress",
        "priority": "urgent",
        "category": BACKEND_ONLY_CATEGORY,
        "requester": "Ravi Nair",
        "assignee": "Ravi Nair",
        "created_at": "2026-09-20T09:00:00Z",
        "updated_at": "2026-09-21T11:30:00Z",
    }
    row.update(overrides)
    return row


def test_a_reported_category_outside_our_enum_is_accepted() -> None:
    """A value we only pass through cannot be validated against a set we invented."""
    ticket = TicketSummary.model_validate(_reported())

    assert ticket.category == BACKEND_ONLY_CATEGORY


def test_one_such_row_does_not_discard_the_whole_page() -> None:
    """The page is the unit of validation, which is where the blast radius came from."""
    result = SearchResult.model_validate(
        {
            "items": [_reported(), _reported(id="TCK-1001", category="hardware")],
            "total": 2,
            "page": 1,
            "page_size": 10,
            "has_more": False,
        }
    )

    assert [item.category for item in result.items] == [BACKEND_ONLY_CATEGORY, "hardware"]


def test_the_detail_view_accepts_it_too() -> None:
    """`Ticket` extends the summary, so the same value must survive the richer model."""
    from ticketing_mcp.models import Ticket

    ticket = Ticket.model_validate(_reported(comments=[]))

    assert ticket.category == BACKEND_ONLY_CATEGORY
    assert ticket.comments == []


def test_a_request_is_still_checked_against_the_closed_set() -> None:
    """The strictness stays where it is a real constraint: the arguments we send.

    A category outside the mock's set is a 422, so accepting one here would move the failure from
    a clear local message to an HTTP error three layers down.
    """
    with pytest.raises(ValidationError):
        NewTicket.model_validate(
            {
                "title": "Network segmentation review",
                "requester": "Ravi Nair",
                "category": BACKEND_ONLY_CATEGORY,
            }
        )

    with pytest.raises(ValidationError):
        SearchQuery.model_validate({"category": BACKEND_ONLY_CATEGORY})

    assert BACKEND_ONLY_CATEGORY not in get_args(Category)
    assert "other" in get_args(Category)
