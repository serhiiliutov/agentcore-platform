"""Writes that are safe to repeat, which the ticketing API does not give us for free.

Stage 1 established two facts that together define the problem:

1. the mock has **no idempotency** — posting the same payload twice creates two tickets;
2. its chaos middleware runs **before** the route handler, so a 429 or 500 wrote nothing,
   while a *timeout* is compatible with the write having succeeded.

Fact 2 divides failures into three groups, and this module treats each differently:

| Outcome | What happened | What this module does |
|---|---|---|
| 429 / 500 / connection refused | provably nothing | release the claim, let `retry.py` retry |
| 401 / 404 / 422 | the request was refused outright | release the claim, raise |
| timeout | **unknown** | keep the claim, verify by reading back |

The third row is the whole reason the module exists. `retry.py` refuses to retry a
timeout, which is correct and leaves the caller with an unanswered question: did the
write land? Guessing wrong in either direction is a real failure — assume it landed and
the user is told their ticket failed when it did not; assume it did not and they get two
tickets. So the module asks the API instead of guessing, and when it cannot get a clear
answer it says so rather than picking one.

**Verification is a search, and searches are heuristic.** A create is verified by
searching for a ticket that matches the submitted payload on every field the caller
supplied, and a comment by fetching the ticket and matching author and body. Two limits
follow from that, and both are deliberate rather than oversights:

* If the same logical ticket already existed before this call, verification can attribute
  that pre-existing ticket to this write. It reuses the caller's own ``client_request_id``
  as nothing more than a ledger key, so it cannot ask the API "which ticket did *my* call
  create" — the mock has no such question to ask.
* When verification finds nothing, or finds more than one match and cannot break the tie,
  it raises `UnresolvedWrite` and **leaves the claim in place**. The next attempt with the
  same key reports ``in_progress`` rather than silently writing again. Surfacing an
  unresolved write to a human beats a confident duplicate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .client import TicketingClient
from .errors import RequestTimeoutError, TicketingError
from .idempotency import Claim, ClaimState, IdempotencyLedger
from .models import (
    MAX_PAGE_SIZE,
    Comment,
    NewComment,
    NewTicket,
    SearchQuery,
    Ticket,
)


class IdempotencyConflictError(TicketingError):
    """A ``client_request_id`` was reused for a *different* request.

    A caller bug, and worth failing loudly: replaying the earlier response would answer
    a question nobody asked, and proceeding would break the guarantee the key exists to
    provide. The fix is a fresh key, not a retry.
    """


class UnresolvedWriteError(TicketingError):
    """A write's outcome could not be determined, so nothing was assumed.

    Raised when a timeout is followed by a verification that cannot decide. The claim
    stays in the ledger, so a later attempt with the same key reports ``in_progress``
    instead of writing again — the state is deliberately left recoverable and not
    silently resolved.

    ``client_request_id`` is carried on the error so an operator or a user can find the
    ledger entry and the ticket it may have created.
    """

    def __init__(
        self,
        message: str,
        *,
        client_request_id: str,
        status: int | None = None,
        code: str | None = None,
        detail: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, status=status, code=code, detail=detail, request_id=request_id)
        self.client_request_id = client_request_id


@dataclass(frozen=True)
class WriteOutcome[T]:
    """A write's result, plus how it was obtained.

    ``replayed`` and ``recovered`` are not decoration. They are what lets a caller tell
    the user the truth: "I created ticket TCK-1007" and "that ticket was already created
    by your earlier request" are different sentences, and a client that cannot distinguish
    them will say the first when it means the second.
    """

    resource: T
    #: Answered from the ledger; the write was not performed again.
    replayed: bool = False
    #: Performed, timed out, and then confirmed by reading the API back.
    recovered: bool = False
    #: True when neither a response nor a verification could confirm the outcome.
    unresolved: bool = False


#: A verification attempt, injected so tests can run it without real delays.
VerifyFn = Callable[[], Awaitable[object | None]]


class IdempotentWriter:
    """Performs the two write operations in a way that survives repetition.

    Wraps `TicketingClient`'s raw write methods. Reads pass straight through and are not
    this class's concern: retrying a read costs a request and cannot corrupt anything,
    which `client.py` already handles.
    """

    def __init__(
        self,
        client: TicketingClient,
        ledger: IdempotencyLedger,
        *,
        scope: str = "tickets",
        verify_attempts: int = 3,
        verify_delay_s: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._ledger = ledger
        self._scope = scope
        #: How many times to re-read while waiting for an in-flight write to land.
        self._verify_attempts = verify_attempts
        #: Gap between those reads. A write that timed out may still be running, so a
        #: single immediate read can legitimately miss it.
        self._verify_delay_s = verify_delay_s
        self._sleep = sleep or asyncio.sleep

    # --- Public operations -------------------------------------------------

    async def create_ticket(
        self, ticket: NewTicket, *, client_request_id: str
    ) -> WriteOutcome[Ticket]:
        """Create a ticket at most once for a given ``client_request_id``."""
        payload = ticket.model_dump(exclude_none=True)
        return await self._guarded(
            key=client_request_id,
            payload=payload,
            model=Ticket,
            perform=lambda: self._client.create_ticket_raw(ticket),
            verify=lambda: self._find_created_ticket(ticket),
        )

    async def add_comment(
        self, ticket_id: str, comment: NewComment, *, client_request_id: str
    ) -> WriteOutcome[Comment]:
        """Add a comment at most once for a given ``client_request_id``."""
        payload = {"ticket_id": ticket_id.upper(), **comment.model_dump(exclude_none=True)}
        return await self._guarded(
            key=client_request_id,
            payload=payload,
            model=Comment,
            perform=lambda: self._client.add_comment_raw(ticket_id, comment),
            verify=lambda: self._find_comment(ticket_id, comment),
        )

    # --- The algorithm -----------------------------------------------------

    async def _guarded[T](
        self,
        *,
        key: str,
        payload: object,
        model: type[T],
        perform: Callable[[], Awaitable[T]],
        verify: VerifyFn,
    ) -> WriteOutcome[T]:
        """Claim, perform, and record — with every ambiguous path resolved explicitly."""
        claim: Claim = self._ledger.reserve(self._scope, key, request_payload=payload)

        if claim.state is ClaimState.REPLAYED:
            # The same request already succeeded. Answer from the record; do not call the
            # API again, which is the entire point of the key.
            return WriteOutcome(resource=_validate(model, claim.response), replayed=True)

        if claim.state is ClaimState.CONFLICT:
            raise IdempotencyConflictError(
                f"client_request_id {key!r} was already used for a different request",
                code="idempotency_conflict",
            )

        if claim.state is ClaimState.IN_PROGRESS:
            # A previous attempt claimed this key and never recorded an outcome - a crash
            # or an unresolved timeout. Never assume it did not happen; look.
            return await self._resolve_in_progress(key, model, verify)

        # CLAIMED: this caller owns the write.
        try:
            resource = await perform()
        except RequestTimeoutError as exc:
            # The request may have been received. Do not release the claim, and do not
            # retry. Read back instead.
            return await self._resolve_timeout(key, model, verify, exc)
        except BaseException:
            # Everything else is provably pre-handler or an outright refusal, so nothing
            # was written. Give the claim back so a corrected retry is not blocked — and
            # re-raise unchanged, because this layer is not the one that decides whether a
            # failure is worth retrying.
            self._ledger.release(self._scope, key)
            raise
        else:
            self._ledger.complete(self._scope, key, _as_payload(resource))
            return WriteOutcome(resource=resource)

    # --- Recovery paths ----------------------------------------------------

    async def _resolve_timeout[T](
        self,
        key: str,
        model: type[T],
        verify: VerifyFn,
        cause: RequestTimeoutError,
    ) -> WriteOutcome[T]:
        """Decide an unanswered write by reading the API back."""
        found = await self._poll(verify)
        if found is None:
            # The claim is deliberately NOT released. Leaving it in_progress is what makes
            # a repeat report 'in progress' rather than writing a second ticket, and it
            # keeps the key recoverable by a later attempt or by a human.
            raise UnresolvedWriteError(
                "the write did not receive a response and the API could not confirm it; "
                "the outcome is unknown, so it was not retried",
                client_request_id=key,
                status=cause.status,
                code="unresolved_write",
                request_id=cause.request_id,
            )
        self._ledger.complete(self._scope, key, _as_payload(found))
        return WriteOutcome(resource=_validate(model, _as_payload(found)), recovered=True)

    async def _resolve_in_progress[T](
        self, key: str, model: type[T], verify: VerifyFn
    ) -> WriteOutcome[T]:
        """Resolve a claim left behind by an earlier attempt."""
        found = await self._poll(verify)
        if found is None:
            raise UnresolvedWriteError(
                "a previous attempt with this client_request_id never completed and the "
                "API cannot confirm it; it was not retried",
                client_request_id=key,
                code="unresolved_write",
            )
        self._ledger.complete(self._scope, key, _as_payload(found))
        return WriteOutcome(resource=_validate(model, _as_payload(found)), recovered=True)

    async def _poll(self, verify: VerifyFn) -> object | None:
        """Ask ``verify`` up to ``verify_attempts`` times, pausing in between.

        A write that timed out may still be executing on the server, so the first read
        can legitimately miss it. Polling a few times before declaring the outcome
        unknown is the difference between a recoverable write and a spurious failure.
        """
        for attempt in range(1, self._verify_attempts + 1):
            found = await verify()
            if found is not None:
                return found
            if attempt < self._verify_attempts:
                await self._sleep(self._verify_delay_s)
        return None

    # --- Verification: the heuristics, and their limits --------------------

    async def _find_created_ticket(self, ticket: NewTicket) -> Ticket | None:
        """Find a ticket matching everything the caller supplied.

        Searched by title and requester rather than by reading every ticket: those are
        the fields the API can filter on, and the rest are compared client-side.

        Returns ``None`` when nothing matches *or* when several match equally closely.
        Ambiguity is reported as "unknown" rather than resolved by picking one, because
        picking wrong here writes a duplicate ticket — the exact harm being avoided.
        """
        page = await self._client.search(
            SearchQuery(
                q=ticket.title,
                requester=ticket.requester,
                page=1,
                page_size=MAX_PAGE_SIZE,
            )
        )

        candidates = [
            item
            for item in page.items
            if item.title == ticket.title
            and item.description == (ticket.description or "")
            and item.priority == ticket.priority
            and item.category == ticket.category
            and item.requester == ticket.requester
        ]

        newest = _pick_newest(candidates)
        if newest is None:
            return None
        # Fetch the detail view so the caller receives comments too, matching what a
        # successful create would have returned.
        return await self._client.get_ticket(newest.id)

    async def _find_comment(self, ticket_id: str, comment: NewComment) -> Comment | None:
        """Find a comment on ``ticket_id`` matching author and body.

        Comments carry no caller-supplied id, so this is a match on content alone. Two
        identical comments on one ticket are indistinguishable here, and are reported as
        ambiguous rather than guessed at.
        """
        ticket = await self._client.get_ticket(ticket_id)
        candidates = [
            existing
            for existing in ticket.comments
            if existing.author == comment.author and existing.body == comment.body
        ]
        return _pick_newest(candidates)


def _pick_newest[T](candidates: list[T]) -> T | None:
    """Return the single newest candidate, or ``None`` if that is not decidable.

    ``created_at`` is ISO-8601 with a trailing ``Z``, so a plain string comparison
    orders it correctly without parsing. A tie on the newest timestamp means two equally
    plausible answers, and the caller treats that as "unknown" rather than choosing.
    """
    if not candidates:
        return None
    ordered = sorted(
        candidates, key=lambda item: str(getattr(item, "created_at", "")), reverse=True
    )
    if len(ordered) > 1 and str(getattr(ordered[0], "created_at", "")) == str(
        getattr(ordered[1], "created_at", "")
    ):
        return None
    return ordered[0]


def _validate[T](model: type[T], payload: object) -> T:
    """Rebuild a model from a ledger entry.

    A stored response that no longer validates means the models changed under an existing
    ledger. That is a real migration problem and is surfaced as such rather than silently
    returning a half-populated object.
    """
    from pydantic import TypeAdapter

    return TypeAdapter(model).validate_python(payload)


def _as_payload(resource: object) -> dict[str, object]:
    """Render a model as a dict for storage."""
    from pydantic import BaseModel

    if isinstance(resource, BaseModel):
        return resource.model_dump()
    raise TypeError(f"expected a pydantic model to store, got {type(resource).__name__}")
