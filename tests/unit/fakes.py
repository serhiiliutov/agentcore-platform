"""A scripted stand-in for the ticketing API, shared by the MCP unit tests.

Lives in a plain module rather than `conftest.py` so both suites can import it directly —
importing a pytest-internal module from a test file is the kind of thing that works until
it does not.

Two suites need it: the guarded-write tests drive the timeout and ambiguity branches
through it, and the tool tests assert what the four tools return for the same situations. A
second copy would drift from the first, and the two would then disagree about what the API
does — the one thing a fake must never do.

It is hand-written rather than a mocking-library double on purpose: the *call count* is the
thing under test in several cases, and an explicit list makes an assertion read as "the API
was asked once".
"""

from __future__ import annotations

from typing import Any

from ticketing_mcp.errors import NotFoundError
from ticketing_mcp.models import (
    Comment,
    NewComment,
    NewTicket,
    SearchQuery,
    SearchResult,
    Ticket,
    TicketSummary,
)

#: Fixed timestamps, so ordering assertions are about logic rather than wall time.
T0 = "2026-10-13T10:00:00Z"
T1 = "2026-10-13T10:05:00Z"


class FakeTicketing:
    """Stands in for `TicketingClient`: records calls, replays scripted faults.

    Implements exactly the `TicketingTransport` protocol, so passing it wherever the real
    client is accepted type-checks instead of needing a cast.
    """

    def __init__(self) -> None:
        self.tickets: dict[str, Ticket] = {}
        self.create_calls: list[NewTicket] = []
        self.comment_calls: list[tuple[str, NewComment]] = []
        self.search_calls: list[SearchQuery] = []
        #: Exceptions the next write should raise, consumed in order.
        self.write_faults: list[BaseException] = []
        self._next_id = 1000

    # --- Seeding: state that exists without this client having called ----------

    def seed_ticket(self, ticket: NewTicket, *, created_at: str = T0) -> Ticket:
        """Add a ticket as though an earlier process had created it.

        Deliberately not routed through `create_ticket_raw`. Seeding is not a call *this*
        client made, so it must not appear in `create_calls` and must not consume a queued
        fault — conflating the two made three tests assert the wrong thing before this
        distinction existed.
        """
        self._next_id += 1
        ticket_id = f"TCK-{self._next_id}"
        created = Ticket(
            id=ticket_id,
            title=ticket.title,
            description=ticket.description,
            status="open",
            priority=ticket.priority,
            category=ticket.category,
            requester=ticket.requester,
            assignee=None,
            created_at=created_at,
            updated_at=created_at,
            comments=[],
        )
        self.tickets[ticket_id] = created
        return created

    def seed_comment(self, ticket_id: str, comment: NewComment, *, created_at: str = T1) -> Comment:
        """Add a comment as though an earlier process had created it."""
        ticket = self.tickets[ticket_id.upper()]
        created = Comment(
            id=f"C-{len(ticket.comments) + 1}",
            author=comment.author,
            body=comment.body,
            created_at=created_at,
        )
        ticket.comments.append(created)
        return created

    # --- The TicketingTransport protocol ---------------------------------------

    async def create_ticket_raw(self, ticket: NewTicket) -> Ticket:
        if self.write_faults:
            raise self.write_faults.pop(0)
        self.create_calls.append(ticket)
        return self.seed_ticket(ticket)

    async def add_comment_raw(self, ticket_id: str, comment: NewComment) -> Comment:
        if self.write_faults:
            raise self.write_faults.pop(0)
        key = ticket_id.upper()
        if key not in self.tickets:
            # The real API 404s for an unknown ticket, so the fake must too; otherwise a
            # tool test would pass against behaviour the mock does not have.
            raise NotFoundError(f"ticket {key} not found", status=404)
        self.comment_calls.append((key, comment))
        return self.seed_comment(key, comment)

    async def search(self, query: SearchQuery) -> SearchResult:
        self.search_calls.append(query)
        items = [
            TicketSummary(
                id=ticket.id,
                title=ticket.title,
                description=ticket.description,
                status=ticket.status,
                priority=ticket.priority,
                category=ticket.category,
                requester=ticket.requester,
                assignee=ticket.assignee,
                created_at=ticket.created_at,
                updated_at=ticket.updated_at,
            )
            for ticket in self.tickets.values()
            if (query.requester is None or ticket.requester.lower() == query.requester.lower())
            and (query.q is None or query.q.lower() in ticket.title.lower())
            and (query.status is None or ticket.status == query.status)
        ]
        return SearchResult(
            items=items,
            total=len(items),
            page=query.page,
            page_size=query.page_size,
            has_more=False,
        )

    async def get_ticket(self, ticket_id: str) -> Ticket:
        key = ticket_id.upper()
        if key not in self.tickets:
            raise NotFoundError(f"ticket {key} not found", status=404)
        return self.tickets[key]


def make_ticket(**overrides: Any) -> NewTicket:
    """A valid ticket, with overrides for whichever field a test is actually about."""
    base: dict[str, Any] = {
        "title": "Printer is offline",
        "description": "The office printer does not respond.",
        "priority": "high",
        "category": "hardware",
        "requester": "Ada Lovelace",
    }
    base.update(overrides)
    return NewTicket(**base)
