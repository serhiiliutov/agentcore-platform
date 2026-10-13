"""What the system actually does when the API it depends on keeps failing.

The unit suite proves the rules; this proves they hold over a socket. That distinction earns
its own file because the two fail differently: a unit test breaks when the logic is wrong, and
these break when the logic is right and the wiring is not — a retry policy that was never
passed to the client, a ledger that is built fresh per call, a timeout that is honoured in
arithmetic and ignored in transport.

Three claims are checked end to end, and they are the three the failure design turns on:

**A failure storm writes nothing.** The mock's chaos runs before its route handlers, so a 429
or a 500 provably did not create a ticket. If that were not true, retrying would be unsound
and the whole retry policy would be a duplicate generator.

**The budget is finite and says so.** A client that retried forever would turn an outage into
a hung request, and one that reported "throttled" after four attempts would invite the reader
to do the thing that has just been established not to work.

**One action is one ticket.** The same `client_request_id` reused after the API recovers must
produce a single ticket, whichever phase asked for it. This is the idempotency ledger doing
its job through a real HTTP round trip rather than through a fake that was written to agree
with it.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest
from pydantic import SecretStr

from tests.integration.conftest import ServedMock
from ticketing_mcp.client import TicketingClient, TicketingConfig
from ticketing_mcp.errors import TicketingError
from ticketing_mcp.idempotency import IdempotencyLedger
from ticketing_mcp.models import NewTicket
from ticketing_mcp.retry import RetryPolicy
from ticketing_mcp.writes import IdempotentWriter

#: Every test here needs a real mock on a real port, which is what the marker means.
pytestmark = pytest.mark.integration

API_KEY = "dev-key-123"

#: Attempts per call. Small on purpose: what is being shown is that the budget is *finite*,
#: and three requests against a mock that fails every one of them demonstrates that as well as
#: thirty would, in a tenth of the time.
ATTEMPTS = 3

#: No waiting between attempts. The backoff is not the subject here, and honouring it would
#: add seconds to each test without changing a single assertion.
NO_DELAY_S = 0.0

#: Every call fails, and the seed makes the 429-versus-500 choice repeatable so a failure run
#: is reproducible rather than merely probable.
ALWAYS_FLAKY: dict[str, str] = {"FLAKY": "1", "FLAKY_RATE": "1.0", "FLAKY_SEED": "42"}


async def _no_sleep(_seconds: float) -> None:
    """Sleep, but not really. The retry delays are irrelevant to every claim here."""
    return


def _client(base_url: str, *, attempts: int = ATTEMPTS) -> TicketingClient:
    """The real client, pointed at a real mock, with a small finite retry budget."""
    return TicketingClient(
        TicketingConfig(
            base_url=base_url,
            api_key=SecretStr(API_KEY),
            timeout_s=5.0,
            retry=RetryPolicy(
                max_attempts=attempts,
                base_delay_s=NO_DELAY_S,
                max_delay_s=NO_DELAY_S,
            ),
        )
    )


def _writer(client: TicketingClient) -> IdempotentWriter:
    """The real guarded writer, with a real ledger and no verification delay.

    The ledger is in-memory here rather than a file. It is still the real ledger — the same
    SQLite claims, the same conflict detection — and a file would only mean cleaning one up
    afterwards.
    """
    return IdempotentWriter(client, IdempotencyLedger(), verify_delay_s=0.0, sleep=_no_sleep)


def _ticket() -> NewTicket:
    """A valid ticket, using only fields whose validity is certain.

    Priority and category are deliberately left at their defaults: they are constrained
    literals in the model, and a test that fails because it guessed at an allowed value is a
    test about the model rather than about failure handling.
    """
    return NewTicket(title="Printer is offline", requester="ada@example.com")


# --- the storm writes nothing ------------------------------------------------


async def test_a_write_that_meets_only_failures_creates_no_ticket(
    serve_mock: Callable[..., ServedMock],
) -> None:
    """The premise every retry in this service rests on, checked over a socket.

    If a failed write *had* reached the handler, retrying it would be how a duplicate is made
    — so the retry policy's safety is not a property of the policy, it is a property of where
    the mock injects its chaos. That is worth proving against the real server rather than
    asserting from the fixture's documentation.
    """
    mock = serve_mock(**ALWAYS_FLAKY)
    before = mock.ticket_count
    client = _client(mock.base_url)

    try:
        with pytest.raises(TicketingError) as raised:
            await _writer(client).create_ticket(_ticket(), client_request_id="req-failure-1")
    finally:
        await client.aclose()

    assert mock.ticket_count == before, "a storm of pre-handler failures must write nothing"
    assert raised.value.attempts == ATTEMPTS, "the whole budget was spent before giving up"


# --- the budget is finite, and honest about itself ---------------------------


async def test_a_read_that_meets_only_failures_gives_up_and_says_how_hard_it_tried(
    serve_mock: Callable[..., ServedMock],
) -> None:
    """The user-facing claim, end to end rather than at the unit boundary.

    "The ticketing API is rate limiting this client" is what a *single* attempt would say, and
    it invites the reader to wait and try again. After three attempts that advice is wrong, so
    the sentence has to carry the history that makes it wrong — and this is where it is
    checked that it does, on the error a caller actually receives.
    """
    mock = serve_mock(**ALWAYS_FLAKY)
    client = _client(mock.base_url)

    try:
        with pytest.raises(TicketingError) as raised:
            await client.get_ticket("TCK-1002")
    finally:
        await client.aclose()

    error = raised.value
    assert error.attempts == ATTEMPTS
    assert str(ATTEMPTS) in error.user_message, (
        "the reader must be told how many attempts were made"
    )
    assert "given up" in error.user_message


async def test_a_failure_storm_does_not_retry_forever(
    serve_mock: Callable[..., ServedMock],
) -> None:
    """An unbounded retry loop would turn an outage into a hung request.

    Timing rather than counting, because counting is what the budget already guarantees and
    the failure being guarded against is a budget that is *consulted* but never *reached* —
    a loop that resets its own counter, or a nested retry that multiplies the two. With the
    delays set to zero, the wall-clock ceiling below is generous enough that only an unbounded
    or badly multiplied loop can exceed it.
    """
    mock = serve_mock(**ALWAYS_FLAKY)
    client = _client(mock.base_url, attempts=5)

    started = time.monotonic()
    try:
        with pytest.raises(TicketingError):
            await client.get_ticket("TCK-1002")
    finally:
        await client.aclose()
    elapsed = time.monotonic() - started

    assert elapsed < 10.0, f"a bounded budget returned in {elapsed:.2f}s, which is not bounded"


# --- one action is one ticket ------------------------------------------------


async def test_the_same_write_after_the_failures_stop_creates_exactly_one_ticket(
    serve_mock: Callable[..., ServedMock],
) -> None:
    """The idempotency claim, through a real HTTP round trip.

    This is the scenario the design exists for, and it is deliberately a *sequence*: the write
    is refused, the API recovers, and the same action is asked for again with the same key.
    The intermediate state is the interesting part — a claim was reserved and released by a
    failure that provably wrote nothing — and a test that only ever saw the healthy case would
    never exercise it.

    The recovery is simulated by switching the mock's chaos flag off, which is a real change of
    backend behaviour and not a mock of one: the middleware reads the flag per request, so the
    next request genuinely succeeds. Waiting for a probabilistic mock to stop failing would make
    this test flaky; choosing a seed whose sequence happens to work would make it unexplainable.
    """
    mock = serve_mock(**ALWAYS_FLAKY)
    before = mock.ticket_count
    client = _client(mock.base_url)
    writer = _writer(client)

    try:
        with pytest.raises(TicketingError):
            await writer.create_ticket(_ticket(), client_request_id="req-failure-2")

        assert mock.ticket_count == before, "the refused attempt must leave nothing behind"

        mock.set_flaky(enabled=False)

        first = await writer.create_ticket(_ticket(), client_request_id="req-failure-2")
        second = await writer.create_ticket(_ticket(), client_request_id="req-failure-2")
    finally:
        await client.aclose()

    assert first.replayed is False, "the first successful call is a real create, not a replay"
    assert first.recovered is False, "nothing had to be verified: the response arrived"
    assert second.replayed is True, "the second call must be answered from the ledger"
    assert mock.ticket_count == before + 1, "one action, one ticket, whichever phase asked"
