"""Unit tests for the guarded write path.

Hermetic: a scripted fake client (`tests.unit.fakes.FakeTicketing`) and an in-memory ledger.
No mock, no HTTP, no Docker.

These tests exist because `writes.py` makes a claim that is easy to state and easy to get
wrong: **a write is performed at most once per `client_request_id`, and an ambiguous
outcome is reported rather than guessed at.** Each test below pins one branch of that claim,
including the branches that are supposed to fail.

The scripted client is what makes the timeout branch testable at all: a real one cannot be
asked to accept a request and then never answer, but a fake can.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.unit.fakes import FakeTicketing
from ticketing_mcp.client import TicketingClient
from ticketing_mcp.errors import RequestTimeoutError, ServerError, ValidationError
from ticketing_mcp.idempotency import IdempotencyLedger
from ticketing_mcp.models import NewComment, NewTicket
from ticketing_mcp.writes import (
    IdempotencyConflictError,
    IdempotentWriter,
    TicketingTransport,
    UnresolvedWriteError,
)


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def writer(transport: FakeTicketing, ledger: IdempotencyLedger) -> IdempotentWriter:
    # No real sleeping: the verification delay is irrelevant to correctness, and waiting
    # would make the suite slow for no gain in coverage.
    return IdempotentWriter(transport, ledger, verify_delay_s=0.0, sleep=_no_sleep)


# --- The happy path, and the replay ----------------------------------------


async def test_a_create_is_performed_once_and_recorded(
    writer: IdempotentWriter,
    transport: FakeTicketing,
    new_ticket: Callable[..., NewTicket],
) -> None:
    outcome = await writer.create_ticket(new_ticket(), client_request_id="req-1")

    assert outcome.resource.id == "TCK-1001"
    assert outcome.replayed is False
    assert outcome.recovered is False
    assert len(transport.create_calls) == 1


async def test_repeating_a_key_replays_without_calling_the_api(
    writer: IdempotentWriter,
    transport: FakeTicketing,
    new_ticket: Callable[..., NewTicket],
) -> None:
    """The property the ledger exists for: the second call is not a second ticket."""
    first = await writer.create_ticket(new_ticket(), client_request_id="req-1")
    second = await writer.create_ticket(new_ticket(), client_request_id="req-1")

    assert second.replayed is True
    assert second.resource.id == first.resource.id
    assert len(transport.create_calls) == 1, "the API must not be asked twice"


async def test_a_different_key_creates_a_second_ticket(
    writer: IdempotentWriter,
    transport: FakeTicketing,
    new_ticket: Callable[..., NewTicket],
) -> None:
    """Two genuine requests still produce two tickets: the guard is not a deduplicator."""
    await writer.create_ticket(new_ticket(), client_request_id="req-1")
    second = await writer.create_ticket(new_ticket(), client_request_id="req-2")

    assert second.replayed is False
    assert len(transport.create_calls) == 2


async def test_reusing_a_key_for_a_different_payload_is_a_conflict(
    writer: IdempotentWriter,
    transport: FakeTicketing,
    new_ticket: Callable[..., NewTicket],
) -> None:
    """Replaying the earlier response would answer a question nobody asked."""
    await writer.create_ticket(new_ticket(), client_request_id="req-1")

    with pytest.raises(IdempotencyConflictError):
        await writer.create_ticket(
            new_ticket(title="A completely different problem"), client_request_id="req-1"
        )

    assert len(transport.create_calls) == 1


# --- Timeouts: the branch this module exists for ----------------------------


async def test_a_timeout_resolved_by_read_back_reports_recovered(
    writer: IdempotentWriter,
    transport: FakeTicketing,
    new_ticket: Callable[..., NewTicket],
) -> None:
    """The write landed but the response was lost.

    The client must not retry (that would create a second ticket) and must not report
    failure (the ticket exists). It reads back and says `recovered`.
    """
    transport.seed_ticket(new_ticket())
    transport.write_faults.append(RequestTimeoutError("gave up waiting"))

    outcome = await writer.create_ticket(new_ticket(), client_request_id="req-1")

    assert outcome.recovered is True
    assert outcome.replayed is False
    assert outcome.resource.title == "Printer is offline"


async def test_a_timeout_with_nothing_found_is_reported_as_unresolved(
    writer: IdempotentWriter,
    transport: FakeTicketing,
    new_ticket: Callable[..., NewTicket],
) -> None:
    """Nothing to find means we genuinely do not know, and the code says so."""
    transport.write_faults.append(RequestTimeoutError("gave up waiting"))

    with pytest.raises(UnresolvedWriteError) as raised:
        await writer.create_ticket(new_ticket(), client_request_id="req-1")

    assert raised.value.client_request_id == "req-1"


async def test_an_unresolved_write_is_not_retried(
    writer: IdempotentWriter,
    transport: FakeTicketing,
    new_ticket: Callable[..., NewTicket],
) -> None:
    """The write must not be performed a second time when its fate is unknown."""
    transport.write_faults.append(RequestTimeoutError("gave up waiting"))

    with pytest.raises(UnresolvedWriteError):
        await writer.create_ticket(new_ticket(), client_request_id="req-1")

    assert transport.create_calls == []


async def test_an_unresolved_write_leaves_its_claim_in_progress(
    writer: IdempotentWriter,
    transport: FakeTicketing,
    new_ticket: Callable[..., NewTicket],
) -> None:
    """Repeating the call must report the unknown state, not silently write again.

    This is the difference between a recoverable key and a duplicate ticket.
    """
    transport.write_faults.append(RequestTimeoutError("gave up waiting"))

    with pytest.raises(UnresolvedWriteError):
        await writer.create_ticket(new_ticket(), client_request_id="req-1")

    with pytest.raises(UnresolvedWriteError):
        await writer.create_ticket(new_ticket(), client_request_id="req-1")

    assert transport.create_calls == []


async def test_a_later_attempt_resolves_a_claim_left_by_a_crash(
    writer: IdempotentWriter,
    ledger: IdempotencyLedger,
    transport: FakeTicketing,
    new_ticket: Callable[..., NewTicket],
) -> None:
    """A claim with no outcome, resolved by finding the ticket a previous attempt made."""
    ticket = new_ticket()
    ledger.reserve("tickets", "req-1", request_payload=ticket.model_dump())
    transport.seed_ticket(ticket)

    outcome = await writer.create_ticket(ticket, client_request_id="req-1")

    assert outcome.recovered is True
    assert transport.create_calls == [], "the existing ticket must be reused, not recreated"


# --- Failures that provably wrote nothing -----------------------------------


async def test_a_server_error_releases_the_claim(
    writer: IdempotentWriter,
    transport: FakeTicketing,
    new_ticket: Callable[..., NewTicket],
) -> None:
    """Chaos runs before the handler, so a 500 wrote nothing.

    Releasing matters: a corrected retry with the same key must not be blocked by a stale
    claim.
    """
    transport.write_faults.append(ServerError("boom", status=500))

    with pytest.raises(ServerError):
        await writer.create_ticket(new_ticket(), client_request_id="req-1")

    outcome = await writer.create_ticket(new_ticket(), client_request_id="req-1")
    assert outcome.replayed is False
    assert len(transport.create_calls) == 1


async def test_a_validation_error_releases_the_claim(
    writer: IdempotentWriter,
    transport: FakeTicketing,
    new_ticket: Callable[..., NewTicket],
) -> None:
    transport.write_faults.append(ValidationError("title too short", status=422))

    with pytest.raises(ValidationError):
        await writer.create_ticket(new_ticket(), client_request_id="req-1")

    # Correctable: a fixed payload under the same key proceeds.
    outcome = await writer.create_ticket(new_ticket(), client_request_id="req-1")
    assert outcome.resource.title == "Printer is offline"


# --- Verification ambiguity --------------------------------------------------


async def test_an_ambiguous_verification_is_reported_rather_than_guessed(
    writer: IdempotentWriter,
    transport: FakeTicketing,
    new_ticket: Callable[..., NewTicket],
) -> None:
    """Two equally-new matches cannot be told apart, so neither is chosen.

    Picking one here is how a duplicate gets mistaken for a success.
    """
    # Identical in every field the verifier compares, and carrying the same timestamp,
    # which is what makes them indistinguishable.
    for _ in range(2):
        transport.seed_ticket(new_ticket())
    transport.write_faults.append(RequestTimeoutError("gave up waiting"))

    with pytest.raises(UnresolvedWriteError):
        await writer.create_ticket(new_ticket(), client_request_id="req-1")


# --- Comments ----------------------------------------------------------------


async def test_a_comment_is_added_once(
    writer: IdempotentWriter,
    transport: FakeTicketing,
    new_ticket: Callable[..., NewTicket],
) -> None:
    transport.seed_ticket(new_ticket())

    outcome = await writer.add_comment(
        "TCK-1001",
        NewComment(author="Grace Hopper", body="Engineer dispatched."),
        client_request_id="req-c1",
    )

    assert outcome.resource.author == "Grace Hopper"
    assert len(transport.comment_calls) == 1


async def test_a_repeated_comment_key_does_not_comment_twice(
    writer: IdempotentWriter,
    transport: FakeTicketing,
    new_ticket: Callable[..., NewTicket],
) -> None:
    transport.seed_ticket(new_ticket())
    comment = NewComment(author="Grace Hopper", body="Engineer dispatched.")

    await writer.add_comment("TCK-1001", comment, client_request_id="req-c1")
    second = await writer.add_comment("TCK-1001", comment, client_request_id="req-c1")

    assert second.replayed is True
    assert len(transport.comment_calls) == 1


async def test_a_comment_timeout_is_recovered_from_the_ticket(
    writer: IdempotentWriter,
    transport: FakeTicketing,
    new_ticket: Callable[..., NewTicket],
) -> None:
    """Comments have no caller-supplied id, so recovery matches on content."""
    ticket = transport.seed_ticket(new_ticket())
    comment = NewComment(author="Grace Hopper", body="Engineer dispatched.")
    transport.seed_comment(ticket.id, comment)
    transport.write_faults.append(RequestTimeoutError("gave up waiting"))

    outcome = await writer.add_comment(ticket.id, comment, client_request_id="req-c1")

    assert outcome.recovered is True


# --- The writer's dependency -------------------------------------------------


def test_both_the_real_client_and_the_fake_satisfy_the_transport_protocol() -> None:
    """The writer types against a protocol, so both sides of it are checked here.

    The fake is what makes the timeout branch testable; the real client is what makes it
    true in production. Verifying only one is how a suite stays green while the shipped path
    breaks.
    """
    assert isinstance(FakeTicketing(), TicketingTransport)
    assert issubclass(TicketingClient, TicketingTransport)
