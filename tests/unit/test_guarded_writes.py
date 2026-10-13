"""Unit tests for the guarded write path.

Hermetic: a scripted fake client and an in-memory ledger. No mock, no HTTP, no Docker.

These tests exist because `writes.py` makes a claim that is easy to state and easy to
get wrong: **a write is performed at most once per `client_request_id`, and an ambiguous
outcome is reported rather than guessed at.** Each test below pins one branch of that
claim, including the branches that are supposed to fail.

The fake client is what makes the timeout branch testable at all: a real one cannot be
asked to accept a request and then never answer, but a scripted one can.
"""

from __future__ import annotations

from typing import Any

import pytest

from ticketing_mcp.client import TicketingClient
from ticketing_mcp.errors import (
    RequestTimeoutError,
    ServerError,
    ValidationError,
)
from ticketing_mcp.idempotency import IdempotencyLedger
from ticketing_mcp.models import (
    Comment,
    NewComment,
    NewTicket,
    SearchQuery,
    SearchResult,
    Ticket,
    TicketSummary,
)
from ticketing_mcp.writes import (
    IdempotencyConflictError,
    IdempotentWriter,
    TicketingTransport,
    UnresolvedWriteError,
)

# --- A scriptable substitute for the API ------------------------------------


class FakeClient:
    """Stands in for `TicketingClient`, recording calls and replaying scripted faults.

    Deliberately not a mock library: the call *count* is the thing under test, and an
    explicit list makes the assertions read as "the API was asked once".
    """

    def __init__(self) -> None:
        self.tickets: dict[str, Ticket] = {}
        self.create_calls: list[NewTicket] = []
        self.comment_calls: list[tuple[str, NewComment]] = []
        #: Exceptions to raise from the next create, consumed in order.
        self.create_faults: list[BaseException] = []
        self._next_id = 1000

    def _mint(self, title: str) -> str:
        self._next_id += 1
        return f"TCK-{self._next_id}"

    def seed_ticket(self, ticket: NewTicket, *, created_at: str = "2026-10-13T10:00:00Z") -> Ticket:
        """Add a ticket as though an earlier process had created it.

        Deliberately not routed through `create_ticket_raw`. Seeding is not a call *this*
        client made, so it must not appear in `create_calls` and must not consume a queued
        fault — several tests need a pre-existing ticket while also scripting a failure,
        and conflating the two made those tests assert the wrong thing.
        """
        ticket_id = self._mint(ticket.title)
        created = Ticket(
            id=ticket_id,
            title=ticket.title,
            description=ticket.description,
            status="open",
            priority=ticket.priority,
            category=ticket.category,
            requester=ticket.requester,
            created_at=created_at,
            updated_at=created_at,
            comments=[],
        )
        self.tickets[ticket_id] = created
        return created

    def seed_comment(
        self,
        ticket_id: str,
        comment: NewComment,
        *,
        created_at: str = "2026-10-13T10:05:00Z",
    ) -> Comment:
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

    async def create_ticket_raw(self, ticket: NewTicket) -> Ticket:
        if self.create_faults:
            raise self.create_faults.pop(0)

        self.create_calls.append(ticket)
        return self.seed_ticket(ticket)

    async def add_comment_raw(self, ticket_id: str, comment: NewComment) -> Comment:
        if self.create_faults:
            raise self.create_faults.pop(0)

        self.comment_calls.append((ticket_id.upper(), comment))
        return self.seed_comment(ticket_id, comment)

    async def search(self, query: SearchQuery) -> SearchResult:
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
            if (query.requester is None or ticket.requester == query.requester)
            and (query.q is None or query.q in ticket.title)
        ]
        return SearchResult(
            items=items,
            total=len(items),
            page=1,
            page_size=query.page_size,
            has_more=False,
        )

    async def get_ticket(self, ticket_id: str) -> Ticket:
        return self.tickets[ticket_id.upper()]


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def ledger() -> IdempotencyLedger:
    return IdempotencyLedger(":memory:")


@pytest.fixture
def writer(client: FakeClient, ledger: IdempotencyLedger) -> IdempotentWriter:
    # No real sleeping: the retry and verification delays are irrelevant to correctness.
    return IdempotentWriter(client, ledger, verify_delay_s=0.0, sleep=_no_sleep)


async def _no_sleep(_seconds: float) -> None:
    return None


def _ticket(**overrides: Any) -> NewTicket:
    base: dict[str, Any] = {
        "title": "Printer is offline",
        "description": "The office printer does not respond.",
        "priority": "high",
        "category": "hardware",
        "requester": "Ada Lovelace",
    }
    base.update(overrides)
    return NewTicket(**base)


# --- The happy path, and the replay ----------------------------------------


async def test_a_create_is_performed_once_and_recorded(
    writer: IdempotentWriter, client: FakeClient
) -> None:
    outcome = await writer.create_ticket(_ticket(), client_request_id="req-1")

    assert outcome.resource.id == "TCK-1001"
    assert outcome.replayed is False
    assert outcome.recovered is False
    assert len(client.create_calls) == 1


async def test_repeating_a_key_replays_from_the_ledger_without_calling_the_api(
    writer: IdempotentWriter, client: FakeClient
) -> None:
    """The property the ledger exists for: the second call is not a second ticket."""
    first = await writer.create_ticket(_ticket(), client_request_id="req-1")
    second = await writer.create_ticket(_ticket(), client_request_id="req-1")

    assert second.replayed is True
    assert second.resource.id == first.resource.id
    assert len(client.create_calls) == 1, "the API must not be asked twice"


async def test_a_replay_is_not_reported_as_a_fresh_create(
    writer: IdempotentWriter,
) -> None:
    """`replayed` is the difference between two true sentences the caller must choose
    between, so it cannot be inferred from the resource alone."""
    await writer.create_ticket(_ticket(), client_request_id="req-1")
    replayed = await writer.create_ticket(_ticket(), client_request_id="req-1")

    assert replayed.replayed is True


async def test_a_different_key_creates_a_second_ticket(
    writer: IdempotentWriter, client: FakeClient
) -> None:
    """Two genuine requests must still produce two tickets. The guard is not a
    deduplicator."""
    await writer.create_ticket(_ticket(), client_request_id="req-1")
    second = await writer.create_ticket(_ticket(), client_request_id="req-2")

    assert second.replayed is False
    assert len(client.create_calls) == 2


# --- Reusing a key for different content ------------------------------------


async def test_reusing_a_key_for_a_different_payload_is_a_conflict(
    writer: IdempotentWriter, client: FakeClient
) -> None:
    """Replaying the earlier response would answer a question nobody asked."""
    await writer.create_ticket(_ticket(), client_request_id="req-1")

    with pytest.raises(IdempotencyConflictError):
        await writer.create_ticket(
            _ticket(title="A completely different problem"), client_request_id="req-1"
        )

    assert len(client.create_calls) == 1


# --- Timeouts: the branch this module exists for ----------------------------


async def test_a_timeout_resolved_by_read_back_reports_recovered(
    writer: IdempotentWriter, client: FakeClient
) -> None:
    """The write landed but the response was lost.

    The client must not retry (that would create a second ticket) and must not report
    failure (the ticket exists). It reads back and says `recovered`.
    """
    # The write lands server-side, then the client gives up before reading the response.
    client.seed_ticket(_ticket())
    client.create_faults.append(RequestTimeoutError("gave up waiting"))

    outcome = await writer.create_ticket(_ticket(), client_request_id="req-1")

    assert outcome.recovered is True
    assert outcome.replayed is False
    assert outcome.resource.title == "Printer is offline"


async def test_a_timeout_with_nothing_found_is_reported_as_unresolved(
    writer: IdempotentWriter, client: FakeClient
) -> None:
    """Nothing to find means we genuinely do not know, and the code says so."""
    client.create_faults.append(RequestTimeoutError("gave up waiting"))

    with pytest.raises(UnresolvedWriteError) as raised:
        await writer.create_ticket(_ticket(), client_request_id="req-1")

    assert raised.value.client_request_id == "req-1"


async def test_an_unresolved_write_is_not_retried(
    writer: IdempotentWriter, client: FakeClient
) -> None:
    """The write must not be performed a second time when its fate is unknown."""
    client.create_faults.append(RequestTimeoutError("gave up waiting"))

    with pytest.raises(UnresolvedWriteError):
        await writer.create_ticket(_ticket(), client_request_id="req-1")

    assert client.create_calls == []


async def test_an_unresolved_write_leaves_its_claim_in_progress(
    writer: IdempotentWriter,
    client: FakeClient,
    ledger: IdempotencyLedger,
) -> None:
    """Repeating the call must report the unknown state, not silently write again.

    This is the difference between a recoverable key and a duplicate ticket.
    """
    client.create_faults.append(RequestTimeoutError("gave up waiting"))

    with pytest.raises(UnresolvedWriteError):
        await writer.create_ticket(_ticket(), client_request_id="req-1")

    with pytest.raises(UnresolvedWriteError):
        await writer.create_ticket(_ticket(), client_request_id="req-1")

    assert client.create_calls == []


async def test_a_later_attempt_resolves_a_claim_left_by_a_crash(
    writer: IdempotentWriter, ledger: IdempotencyLedger, client: FakeClient
) -> None:
    """A claim with no outcome, resolved by finding the ticket a previous attempt made."""
    # Simulate an earlier process that claimed the key and died mid-write.
    ledger.reserve("tickets", "req-1", request_payload=_ticket().model_dump())
    client.seed_ticket(_ticket())

    outcome = await writer.create_ticket(_ticket(), client_request_id="req-1")

    assert outcome.recovered is True
    assert client.create_calls == [], "the existing ticket must be reused, not recreated"


# --- Failures that provably wrote nothing -----------------------------------


async def test_a_server_error_releases_the_claim(
    writer: IdempotentWriter, ledger: IdempotencyLedger, client: FakeClient
) -> None:
    """Chaos runs before the handler, so a 500 wrote nothing.

    Releasing matters: a corrected retry with the same key must not be blocked by a
    stale claim.
    """
    client.create_faults.append(ServerError("boom", status=500))

    with pytest.raises(ServerError):
        await writer.create_ticket(_ticket(), client_request_id="req-1")

    # The claim is gone, so the same key can be used again.
    outcome = await writer.create_ticket(_ticket(), client_request_id="req-1")
    assert outcome.replayed is False
    assert len(client.create_calls) == 1


async def test_a_validation_error_releases_the_claim(
    writer: IdempotentWriter, client: FakeClient
) -> None:
    client.create_faults.append(ValidationError("title too short", status=422))

    with pytest.raises(ValidationError):
        await writer.create_ticket(_ticket(), client_request_id="req-1")

    # Correctable: a fixed payload under the same key proceeds.
    outcome = await writer.create_ticket(_ticket(), client_request_id="req-1")
    assert outcome.resource.title == "Printer is offline"


# --- Verification ambiguity --------------------------------------------------


async def test_an_ambiguous_verification_is_reported_rather_than_guessed(
    writer: IdempotentWriter, client: FakeClient
) -> None:
    """Two equally-new matches cannot be told apart, so neither is chosen.

    Picking one here is how a duplicate gets mistaken for a success.
    """
    # Two tickets identical in every field the verifier compares, and - crucially -
    # carrying the same timestamp, which is what makes them indistinguishable.
    for _ in range(2):
        client.seed_ticket(_ticket())
    client.create_faults.append(RequestTimeoutError("gave up waiting"))

    with pytest.raises(UnresolvedWriteError):
        await writer.create_ticket(_ticket(), client_request_id="req-1")


# --- Comments ----------------------------------------------------------------


async def test_a_comment_is_added_once(writer: IdempotentWriter, client: FakeClient) -> None:
    await client.create_ticket_raw(_ticket())

    outcome = await writer.add_comment(
        "TCK-1001",
        NewComment(author="Grace Hopper", body="Engineer dispatched."),
        client_request_id="req-c1",
    )

    assert outcome.resource.author == "Grace Hopper"
    assert len(client.comment_calls) == 1


async def test_a_repeated_comment_key_does_not_comment_twice(
    writer: IdempotentWriter, client: FakeClient
) -> None:
    await client.create_ticket_raw(_ticket())
    comment = NewComment(author="Grace Hopper", body="Engineer dispatched.")

    await writer.add_comment("TCK-1001", comment, client_request_id="req-c1")
    second = await writer.add_comment("TCK-1001", comment, client_request_id="req-c1")

    assert second.replayed is True
    assert len(client.comment_calls) == 1


async def test_a_comment_timeout_is_recovered_from_the_ticket(
    writer: IdempotentWriter, client: FakeClient
) -> None:
    """Comments have no caller-supplied id, so recovery matches on content."""
    ticket = client.seed_ticket(_ticket())
    comment = NewComment(author="Grace Hopper", body="Engineer dispatched.")
    client.seed_comment(ticket.id, comment)

    client.create_faults.append(RequestTimeoutError("gave up waiting"))

    outcome = await writer.add_comment("TCK-1001", comment, client_request_id="req-c1")

    assert outcome.recovered is True


# --- Shape of the writer's dependency ---------------------------------------


def test_both_the_real_client_and_the_fake_satisfy_the_transport_protocol() -> None:
    """The writer types against a protocol, so both sides of it are checked here.

    The fake is what makes the timeout branch testable; the real client is what makes it
    true in production. Verifying only one of them is how a suite stays green while the
    shipped path breaks - so the real class is asserted against the same protocol the
    writer is written against.
    """
    assert isinstance(FakeClient(), TicketingTransport)
    assert issubclass(TicketingClient, TicketingTransport)
