"""The four tools, as plain async functions with no MCP plumbing attached.

`server.py` registers these with `MCPServer`; this module knows nothing about the MCP
protocol. The split exists because tool *logic* — which arguments mean what, what a
failure looks like to a model — is worth testing directly, and testing it through a
protocol handshake would make every test a slow integration test.

**Failures are returned, not raised.** Every function here returns a `ToolOutcome` whose
`is_error` flag the server turns into `CallToolResult.is_error`. A raised exception would
cross the protocol boundary as a transport-level fault, and the agent would lose the one
thing it needs to recover: *which* kind of failure this was. A model told "no such
ticket TCK-9" behaves differently from one told "the backend is down, try again", and
only the returned outcome can carry that distinction.

**Writes require a `client_request_id` from the caller.** The tool does not invent one.
That is deliberate: the idempotency key has to be decided *before* the write is approved,
so that re-executing an approved action reuses the same key and cannot create a second
ticket. A key minted inside the tool would be new on every execution and would defeat the
entire guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import TicketingError
from .models import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    NewComment,
    NewTicket,
    SearchQuery,
)
from .writes import IdempotencyConflictError, IdempotentWriter, TicketingTransport


#: What a tool hands back to the server, before it becomes a CallToolResult.
@dataclass(frozen=True)
class ToolOutcome:
    """A tool's result, transport-agnostic.

    ``payload`` is the structured data the model reasons over; ``is_error`` says whether
    to treat it as a failure. ``message`` is the human-readable line that accompanies it,
    written for a model rather than a log: it names what went wrong in terms a model can
    act on, and never includes a credential or an internal stack.
    """

    payload: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False
    message: str = ""

    @classmethod
    def ok(cls, payload: dict[str, Any], message: str = "") -> ToolOutcome:
        return cls(payload=payload, is_error=False, message=message)

    @classmethod
    def failed(cls, error: TicketingError, message: str | None = None) -> ToolOutcome:
        """Turn an exception into a result the model can reason about.

        ``error.as_dict()`` is used rather than ``str(error)`` because it carries the
        machine-readable kind alongside the text, so a model can distinguish a missing
        ticket from a refused one without parsing prose.
        """
        detail = error.as_dict()
        return cls(
            payload={"error": detail},
            is_error=True,
            message=message or _describe(error),
        )


def _describe(error: TicketingError) -> str:
    """A model-facing sentence for a known failure.

    Written per failure *kind* rather than per status code, because what a model should
    do next depends on the kind: a validation error means fix the arguments, a
    not-found means the ticket does not exist, and a rate limit means wait.
    """
    from .errors import AuthenticationError, NotFoundError, RateLimitedError, ValidationError
    from .writes import UnresolvedWriteError

    if isinstance(error, NotFoundError):
        return "No ticket with that id exists. Check the id before retrying."
    if isinstance(error, ValidationError):
        return f"The request was rejected: {error.detail or error.message}. Fix the arguments."
    if isinstance(error, AuthenticationError):
        return "The ticketing API rejected the credential. This is a configuration problem, not a retry."
    if isinstance(error, RateLimitedError):
        return (
            "The ticketing API is rate limiting this client. Waiting is required before retrying."
        )
    if isinstance(error, IdempotencyConflictError):
        return (
            "This client_request_id was already used for a different request. "
            "Use a new key for a new action."
        )
    if isinstance(error, UnresolvedWriteError):
        return (
            "The write's outcome could not be confirmed and it was NOT retried, because it "
            "may have succeeded. Check the ticket list before deciding whether to try again."
        )
    return error.message


# --- Reads ------------------------------------------------------------------


async def search_tickets(
    client: TicketingTransport,
    *,
    q: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    requester: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> ToolOutcome:
    """Find tickets. Returns one page plus the arithmetic needed to describe it.

    ``total`` and ``has_more`` are included in the payload on purpose. Without them a
    model sees ten rows and cannot tell "these are all the matches" from "these are the
    first ten of forty", and will state the wrong thing with confidence.
    """
    try:
        query = SearchQuery(
            q=q,
            status=status,  # type: ignore[arg-type]
            priority=priority,  # type: ignore[arg-type]
            category=category,  # type: ignore[arg-type]
            requester=requester,
            page=page,
            page_size=min(page_size, MAX_PAGE_SIZE),
        )
    except ValueError as exc:
        # Pydantic's validation error, surfaced as a fixable argument problem.
        return ToolOutcome(
            payload={"error": {"kind": "InvalidArguments", "detail": str(exc)}},
            is_error=True,
            message=f"The search arguments are invalid: {exc}",
        )

    try:
        result = await client.search(query)
    except TicketingError as exc:
        return ToolOutcome.failed(exc)

    return ToolOutcome.ok(
        {
            "items": [item.model_dump() for item in result.items],
            "total": result.total,
            "page": result.page,
            "page_size": result.page_size,
            "has_more": result.has_more,
        },
        f"{len(result.items)} of {result.total} matching ticket(s) on page {result.page}.",
    )


async def get_ticket(client: TicketingTransport, *, ticket_id: str) -> ToolOutcome:
    """Fetch one ticket, including its comments."""
    try:
        ticket = await client.get_ticket(ticket_id)
    except TicketingError as exc:
        return ToolOutcome.failed(exc)

    return ToolOutcome.ok(
        ticket.model_dump(),
        f"Ticket {ticket.id}: {ticket.title} [{ticket.status}] with "
        f"{len(ticket.comments)} comment(s).",
    )


# --- Writes, through the guarded path ---------------------------------------


async def create_ticket(
    writer: IdempotentWriter,
    *,
    client_request_id: str,
    title: str,
    description: str = "",
    priority: str = "medium",
    category: str = "other",
    requester: str,
) -> ToolOutcome:
    """Create a ticket, at most once per ``client_request_id``."""
    try:
        ticket = NewTicket(
            title=title,
            description=description,
            priority=priority,  # type: ignore[arg-type]
            category=category,  # type: ignore[arg-type]
            requester=requester,
        )
    except ValueError as exc:
        return ToolOutcome(
            payload={"error": {"kind": "InvalidArguments", "detail": str(exc)}},
            is_error=True,
            message=f"The ticket is not valid: {exc}",
        )

    try:
        outcome = await writer.create_ticket(ticket, client_request_id=client_request_id)
    except TicketingError as exc:
        return ToolOutcome.failed(exc)

    payload = outcome.resource.model_dump()
    # `replayed` and `recovered` are surfaced so the caller can tell the user which of
    # three true sentences applies rather than always claiming a fresh create.
    payload["idempotency"] = {
        "client_request_id": client_request_id,
        "replayed": outcome.replayed,
        "recovered": outcome.recovered,
    }
    if outcome.replayed:
        message = (
            f"Ticket {outcome.resource.id} already existed for this request; nothing was created."
        )
    elif outcome.recovered:
        message = f"Ticket {outcome.resource.id} was created, confirmed by reading it back."
    else:
        message = f"Created ticket {outcome.resource.id}."
    return ToolOutcome.ok(payload, message)


async def add_comment(
    writer: IdempotentWriter,
    *,
    client_request_id: str,
    ticket_id: str,
    author: str,
    body: str,
) -> ToolOutcome:
    """Add a comment to a ticket, at most once per ``client_request_id``."""
    try:
        comment = NewComment(author=author, body=body)
    except ValueError as exc:
        return ToolOutcome(
            payload={"error": {"kind": "InvalidArguments", "detail": str(exc)}},
            is_error=True,
            message=f"The comment is not valid: {exc}",
        )

    try:
        outcome = await writer.add_comment(ticket_id, comment, client_request_id=client_request_id)
    except TicketingError as exc:
        return ToolOutcome.failed(exc)

    payload = {
        "ticket_id": ticket_id.upper(),
        "comment": outcome.resource.model_dump(),
        "idempotency": {
            "client_request_id": client_request_id,
            "replayed": outcome.replayed,
            "recovered": outcome.recovered,
        },
    }
    if outcome.replayed:
        message = f"That comment was already added to {ticket_id.upper()}; nothing was added."
    else:
        message = f"Comment added to {ticket_id.upper()}."
    return ToolOutcome.ok(payload, message)
