"""Unit tests for the four tools and the server that exposes them.

Hermetic: the shared scripted transport from `fakes.py` plus an in-memory ledger. No mock,
no HTTP, no Docker.

Two things are being pinned here. First, that each tool reports the *kind* of failure it hit
rather than a generic error, because that is what lets an agent decide whether to fix its
arguments, accept a missing record, or wait. Second, that the write tools refuse to invent a
`client_request_id` — the whole approval guarantee depends on the key being decided by the
caller before the write is authorized.

The server test asserts on the registered tool *schemas*, which is the one thing that cannot
be checked by calling the functions directly: a tool registered with the wrong name, or
without the `client_request_id` parameter, is broken even though its implementation is fine.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import SecretStr

from tests.unit.fakes import FakeTicketing
from ticketing_mcp.config import ServerSettings
from ticketing_mcp.idempotency import IdempotencyLedger
from ticketing_mcp.models import NewTicket
from ticketing_mcp.server import build_server
from ticketing_mcp.tools import (
    add_comment,
    create_ticket,
    get_ticket,
    search_tickets,
)
from ticketing_mcp.writes import IdempotentWriter


@pytest.fixture
def writer(transport: FakeTicketing, ledger: IdempotencyLedger) -> IdempotentWriter:
    return IdempotentWriter(transport, ledger, verify_delay_s=0.0, sleep=_no_sleep)


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def settings() -> ServerSettings:
    return ServerSettings(
        base_url="http://127.0.0.1:9",
        api_key=SecretStr("test-key"),
        # A Path, not a string: the field is typed as Path, and the fake transport plus a
        # supplied ledger mean this value is never opened.
        ledger_path=Path("unused.sqlite3"),
        timeout_s=1.0,
    )


# --- search_tickets ---------------------------------------------------------


async def test_search_returns_the_page_arithmetic(
    transport: FakeTicketing, new_ticket: Callable[..., NewTicket]
) -> None:
    """A model must be able to tell ten-of-ten from ten-of-forty."""
    transport.seed_ticket(new_ticket())

    outcome = await search_tickets(transport)

    assert outcome.is_error is False
    assert outcome.payload["total"] == 1
    assert outcome.payload["has_more"] is False
    assert len(outcome.payload["items"]) == 1


async def test_search_filters_by_requester_exactly(
    transport: FakeTicketing, new_ticket: Callable[..., NewTicket]
) -> None:
    transport.seed_ticket(new_ticket(requester="Ada Lovelace"))
    transport.seed_ticket(new_ticket(requester="Grace Hopper", title="Network down"))

    outcome = await search_tickets(transport, requester="ada lovelace")

    assert outcome.payload["total"] == 1
    assert outcome.payload["items"][0]["requester"] == "Ada Lovelace"


async def test_search_caps_an_oversized_page_size(
    transport: FakeTicketing, new_ticket: Callable[..., NewTicket]
) -> None:
    """The API 422s above the maximum rather than truncating, so the tool clamps first."""
    transport.seed_ticket(new_ticket())

    await search_tickets(transport, page_size=9999)

    assert transport.search_calls[0].page_size == 50


async def test_search_rejects_an_invalid_status_as_an_argument_error(
    transport: FakeTicketing,
) -> None:
    """An unknown enum value is a fixable argument problem, not an empty result."""
    outcome = await search_tickets(transport, status="not-a-status")

    assert outcome.is_error is True
    assert outcome.payload["error"]["kind"] == "InvalidArguments"
    assert transport.search_calls == []


# --- get_ticket -------------------------------------------------------------


async def test_get_ticket_returns_comments(
    transport: FakeTicketing, new_ticket: Callable[..., NewTicket]
) -> None:
    from ticketing_mcp.models import NewComment

    ticket = transport.seed_ticket(new_ticket())
    transport.seed_comment(ticket.id, NewComment(author="Grace", body="On it."))

    outcome = await get_ticket(transport, ticket_id=ticket.id)

    assert outcome.is_error is False
    assert len(outcome.payload["comments"]) == 1


async def test_get_ticket_reports_a_missing_ticket_distinctly(
    transport: FakeTicketing,
) -> None:
    """'No such ticket' and 'the backend is down' call for different next actions."""
    outcome = await get_ticket(transport, ticket_id="TCK-9999")

    assert outcome.is_error is True
    assert outcome.payload["error"]["kind"] == "NotFoundError"
    assert "No ticket with that id exists" in outcome.message


# --- create_ticket ----------------------------------------------------------


async def test_create_reports_a_fresh_ticket(
    writer: IdempotentWriter, new_ticket: Callable[..., NewTicket]
) -> None:
    outcome = await create_ticket(
        writer,
        client_request_id="req-1",
        title="Printer is offline",
        requester="Ada Lovelace",
        priority="high",
    )

    assert outcome.is_error is False
    assert outcome.payload["idempotency"]["replayed"] is False
    assert "Created ticket" in outcome.message


async def test_create_reports_a_replay_as_already_done(
    writer: IdempotentWriter,
) -> None:
    """The caller must be able to say 'already created', not claim a second ticket."""
    first = await create_ticket(
        writer, client_request_id="req-1", title="Printer is offline", requester="Ada Lovelace"
    )
    second = await create_ticket(
        writer, client_request_id="req-1", title="Printer is offline", requester="Ada Lovelace"
    )

    assert second.payload["id"] == first.payload["id"]
    assert second.payload["idempotency"]["replayed"] is True
    assert "already existed" in second.message


async def test_create_rejects_an_invalid_title_as_an_argument_error(
    writer: IdempotentWriter,
) -> None:
    outcome = await create_ticket(
        writer, client_request_id="req-1", title="no", requester="Ada Lovelace"
    )

    assert outcome.is_error is True
    assert outcome.payload["error"]["kind"] == "InvalidArguments"


async def test_create_requires_the_caller_to_supply_the_key(
    writer: IdempotentWriter,
) -> None:
    """Making the key mandatory is what makes an approved write safe to re-execute."""
    import inspect

    parameters = inspect.signature(create_ticket).parameters
    assert "client_request_id" in parameters
    assert parameters["client_request_id"].default is inspect.Parameter.empty


# --- add_comment ------------------------------------------------------------


async def test_add_comment_succeeds_on_an_existing_ticket(
    writer: IdempotentWriter, transport: FakeTicketing, new_ticket: Callable[..., NewTicket]
) -> None:
    ticket = transport.seed_ticket(new_ticket())

    outcome = await add_comment(
        writer,
        client_request_id="req-c1",
        ticket_id=ticket.id,
        author="Grace Hopper",
        body="Engineer dispatched.",
    )

    assert outcome.is_error is False
    assert outcome.payload["ticket_id"] == ticket.id
    assert len(transport.comment_calls) == 1


async def test_add_comment_on_a_missing_ticket_is_a_not_found(
    writer: IdempotentWriter,
) -> None:
    outcome = await add_comment(
        writer,
        client_request_id="req-c1",
        ticket_id="TCK-9999",
        author="Grace Hopper",
        body="Engineer dispatched.",
    )

    assert outcome.is_error is True
    assert outcome.payload["error"]["kind"] == "NotFoundError"


# --- The server registers the tools correctly --------------------------------


async def test_the_server_registers_exactly_the_four_expected_tools(
    settings: ServerSettings, transport: FakeTicketing, ledger: IdempotencyLedger
) -> None:
    """Asserting on schemas, not on behaviour: a misnamed tool is broken even if its
    function body is correct."""
    server, _ = build_server(settings, client=transport, ledger=ledger)

    tools = await server.list_tools()

    assert {tool.name for tool in tools} == {
        "search_tickets",
        "get_ticket",
        "create_ticket",
        "add_comment",
    }


async def test_the_write_tools_publish_client_request_id_as_required(
    settings: ServerSettings, transport: FakeTicketing, ledger: IdempotencyLedger
) -> None:
    """The schema is the contract the model sees. If the key were optional in the schema,
    a model could omit it and the approval guarantee would be unreachable."""
    server, _ = build_server(settings, client=transport, ledger=ledger)

    tools = {tool.name: tool for tool in await server.list_tools()}

    for name in ("create_ticket", "add_comment"):
        schema = tools[name].input_schema
        assert "client_request_id" in schema["required"], f"{name} must require the key"


async def test_the_read_tools_do_not_require_a_key(
    settings: ServerSettings, transport: FakeTicketing, ledger: IdempotencyLedger
) -> None:
    """Reads are safe to repeat, so demanding a key for them would be noise."""
    server, _ = build_server(settings, client=transport, ledger=ledger)

    tools = {tool.name: tool for tool in await server.list_tools()}

    for name in ("search_tickets", "get_ticket"):
        assert "client_request_id" not in tools[name].input_schema.get("required", [])
