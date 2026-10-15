"""The MCP server: four tools, two transports, one process.

Stage 3 established the API this is built on. `FastMCP` no longer exists in mcp 2.x; the
class is `MCPServer`, `@server.tool(...)` registers a tool, and a single
`server.run(transport=...)` covers both `stdio` and `streamable-http`. That last point is
what makes ADR-0001's "both transports, one codebase" real rather than aspirational — the
tool definitions are written once and neither transport knows about the other.

**The tool descriptions are part of the implementation.** They are the only thing the
agent has to decide when to call a tool and with what, so they state the semantics rather
than restating the parameter names: that `q` searches text while `requester` is an exact
name match, that the page size is capped rather than silently truncated, and that the write
tools require a `client_request_id` the caller must reuse on re-execution. A tool whose
description omits those will be used wrongly, and no amount of validation downstream
recovers the agent's intent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from .client import TicketingClient, TicketingConfig
from .config import ServerSettings
from .idempotency import IdempotencyLedger
from .models import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Category,
    Priority,
    Status,
)
from .tools import (
    ToolOutcome,
    add_comment,
    create_ticket,
    get_ticket,
    search_tickets,
)
from .writes import IdempotentWriter, TicketingTransport

#: Reported to clients on initialize.
SERVER_NAME = "ticketing"

#: Sent to the agent on initialize. This is where the write contract is explained once,
#: rather than only in each tool description.
INSTRUCTIONS = """\
Tools for a ticketing system: search and read tickets, create tickets, and comment on them.

Reads are free to repeat. Writes are not: `create_ticket` and `add_comment` each require a
`client_request_id` that YOU choose, and you must reuse the same value if you re-execute the
same action. Reusing a key for the same action can never create a second ticket; using a new
key for the same action WILL. A write result reports `replayed` or `recovered` when the work
was already done, so report what happened rather than assuming a fresh write.

Neither write tool asks for confirmation. Deciding whether a write should happen belongs to
the caller, not to this server."""


def _options(literal: Any) -> str:
    """The values of a ``Literal`` as a readable list.

    Read from the ``Literal`` the tool itself validates against rather than typed out beside it. A
    hand-written list in a description and a hand-written set of allowed values are one fact in two
    places, and the description is the one that goes stale — so the model is told a value the server
    will then reject.
    """
    return ", ".join(f"`{value}`" for value in get_args(literal))


#: The closed sets, rendered once each from the model that enforces them.
_STATUS_VALUES = _options(Status)
_PRIORITY_VALUES = _options(Priority)
_CATEGORY_VALUES = _options(Category)


def to_call_tool_result(outcome: ToolOutcome) -> CallToolResult:
    """Turn a `ToolOutcome` into the MCP result type.

    Both representations are populated on purpose. ``content`` carries a short sentence
    for a model reading text; ``structured_content`` carries the same data as JSON for a
    client that wants to reason over fields. A tool that returned only one of them would
    be worse for half its callers.
    """
    body = {"message": outcome.message, "data": outcome.payload}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body, indent=2, default=str))],
        structured_content=body,
        is_error=outcome.is_error,
    )


def build_server(
    settings: ServerSettings,
    *,
    client: TicketingTransport | None = None,
    ledger: IdempotencyLedger | None = None,
) -> tuple[MCPServer[Any], TicketingTransport]:
    """Wire the four tools onto an `MCPServer` and return it with its client.

    ``client`` is typed as the `TicketingTransport` protocol rather than the concrete
    `TicketingClient` for the same reason the writer is: a test can then inject a scripted
    transport and assert on the registered tool schemas without a live server or a cast,
    while `_assert_transport_conformance` in `writes.py` keeps the real client checked
    against the same protocol.
    """
    ticketing = client or TicketingClient(
        TicketingConfig(
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout_s=settings.timeout_s,
        )
    )

    if ledger is None:
        _ensure_parent(settings.ledger_path)
        ledger = IdempotencyLedger(settings.ledger_path)

    writer = IdempotentWriter(ticketing, ledger, scope="mcp")

    server: MCPServer[Any] = MCPServer(
        name=SERVER_NAME,
        version=settings.version,
        instructions=INSTRUCTIONS,
    )

    @server.tool(
        name="search_tickets",
        description=(
            "Search tickets. Returns one page of results plus `total` and `has_more`, so "
            "you can tell whether more matches exist before saying a result is complete. "
            "Use `q` for a substring match across title and description; use `requester` "
            "for an exact (case-insensitive) name match. They compose. "
            "The filters are exact matches against closed sets, and an unknown value is "
            f"rejected rather than ignored, so do not guess: `status` is one of {_STATUS_VALUES}; "
            f"`priority` is one of {_PRIORITY_VALUES}; "
            f"`category` is one of {_CATEGORY_VALUES}. "
            f"`page_size` is capped at {MAX_PAGE_SIZE} by the API rather than truncated. "
            "To count, group or summarise results — 'how many are open', 'the split by "
            f"category' — set `page_size` to `total` (no more than {MAX_PAGE_SIZE}) and keep "
            "reading pages until `has_more` is false, then count what you received. Never "
            "scale or estimate from one page: a row you did not retrieve is a row you do not "
            "know about, and a count derived from the first page of several is wrong."
        ),
    )
    async def search_tickets_tool(
        q: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        category: str | None = None,
        requester: str | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> CallToolResult:
        return to_call_tool_result(
            await search_tickets(
                ticketing,
                q=q,
                status=status,
                priority=priority,
                category=category,
                requester=requester,
                page=page,
                page_size=page_size,
            )
        )

    @server.tool(
        name="get_ticket",
        description=(
            "Fetch one ticket by id, including its comments. Prefer this over searching "
            "when you already know the id: it is exact, and it returns the comments that "
            "search results omit. A ticket id looks like TCK-1001; case does not matter."
        ),
    )
    async def get_ticket_tool(ticket_id: str) -> CallToolResult:
        return to_call_tool_result(await get_ticket(ticketing, ticket_id=ticket_id))

    @server.tool(
        name="create_ticket",
        description=(
            "Create a ticket. Requires `client_request_id`: choose a value when you decide "
            "to perform this write and REUSE it if you execute the same action again, so a "
            "retry cannot create a second ticket. Treat a result marked `replayed` as "
            "'already done', not as a new ticket. "
            f"`priority` is one of {_PRIORITY_VALUES} (default `medium`); "
            f"`category` is one of {_CATEGORY_VALUES} (default `other`); "
            f"`title` must be {3}-{120} characters; `requester` is required."
        ),
    )
    async def create_ticket_tool(
        client_request_id: str,
        title: str,
        requester: str,
        description: str = "",
        priority: str = "medium",
        category: str = "other",
    ) -> CallToolResult:
        return to_call_tool_result(
            await create_ticket(
                writer,
                client_request_id=client_request_id,
                title=title,
                description=description,
                priority=priority,
                category=category,
                requester=requester,
            )
        )

    @server.tool(
        name="add_comment",
        description=(
            "Add a comment to an existing ticket. Requires `client_request_id` with the "
            "same reuse rule as `create_ticket`. Fails if the ticket does not exist, so "
            "confirm the id first if you are unsure."
        ),
    )
    async def add_comment_tool(
        client_request_id: str,
        ticket_id: str,
        author: str,
        body: str,
    ) -> CallToolResult:
        return to_call_tool_result(
            await add_comment(
                writer,
                client_request_id=client_request_id,
                ticket_id=ticket_id,
                author=author,
                body=body,
            )
        )

    return server, ticketing


def _ensure_parent(path: Path) -> None:
    """Create the ledger's directory if it is missing.

    SQLite will happily create the file but not the directory, and the failure arrives as
    an opaque `unable to open database file` on the first write rather than at startup.
    """
    if path.parent and str(path.parent) not in {"", "."}:
        path.parent.mkdir(parents=True, exist_ok=True)
