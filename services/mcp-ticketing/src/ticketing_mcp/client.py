"""HTTP transport for the ticketing API.

This module knows how to *talk* to the mock. It does not know about approvals — that is
the agent's concern, one layer up — and it does not own the idempotency decision, which
lives in `writes.py` because it needs a ledger the transport should not have to carry.

What it does own is the translation between HTTP and this package's vocabulary:

* every non-2xx response becomes an exception from `errors.py`, in one place, so no call
  site parses a body itself;
* every retryable failure goes through `retry.call_with_retry`, so the policy is applied
  uniformly rather than at whichever call sites remembered to think about it;
* a timeout becomes `RequestTimeoutError` and is **not** retried — see `retry.py`.

Timeouts deserve one note. ``httpx.Timeout`` distinguishes connect, read, write and pool
phases. Only the read timeout produces the interesting case, because that is the one a
slow handler triggers: the request was received and the response was late. A connect
timeout means no connection was ever established, so nothing happened. Both surface as
`RequestTimeoutError` here, which is the conservative choice — the caller verifies rather
than assumes, and verification is cheap for a read and necessary for a write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, SecretStr, ValidationError as PydanticValidationError

from . import __version__
from .errors import (
    ConfigurationError,
    MalformedResponseError,
    RequestTimeoutError,
    TransportError,
    error_from_response,
)
from .models import (
    Comment,
    NewComment,
    NewTicket,
    SearchQuery,
    SearchResult,
    Ticket,
)
from .retry import RetryPolicy, call_with_retry


@dataclass(frozen=True)
class TicketingConfig:
    """Where the API is and how to reach it."""

    base_url: str
    api_key: SecretStr
    #: Per-phase HTTP timeout. Deliberately shorter than the mock's usual latency so a
    #: fault-injection run can actually produce a timeout rather than hanging.
    timeout_s: float = 10.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    def __post_init__(self) -> None:
        if not self.base_url.strip():
            raise ConfigurationError("base_url is required")
        if not self.api_key.get_secret_value().strip():
            raise ConfigurationError("api_key is required")


class TicketingClient:
    """An async client for the mock ticketing API.

    The ``httpx.AsyncClient`` is injectable so tests can supply a transport that returns
    scripted responses, including faults, without a live server.
    """

    def __init__(
        self,
        config: TicketingConfig,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            timeout=httpx.Timeout(config.timeout_s),
            headers={
                # The key travels in a header, never a query string, so it cannot end up
                # in a URL that gets logged by an intermediary.
                "X-API-Key": config.api_key.get_secret_value(),
                "User-Agent": f"agentcore-mcp/{__version__}",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        """Close the underlying client if this instance created it."""
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> TicketingClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # --- One place where HTTP becomes exceptions ---------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Perform one request, translating failures.

        Retrying happens *around* this method rather than inside it, so the retry policy
        applies to network failures identically for every verb.
        """

        async def attempt() -> httpx.Response:
            try:
                return await self._http.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                # Includes connect, read, write and pool timeouts. See the module
                # docstring for why one class is the safe default.
                raise RequestTimeoutError(
                    f"{method} {path} did not complete within the client timeout"
                ) from exc
            except httpx.TransportError as exc:
                raise TransportError(f"{method} {path} failed at the transport: {exc}") from exc

        response = await call_with_retry(attempt, policy=self._config.retry)

        if response.status_code >= 400:
            raise error_from_response(
                response.status_code,
                _safe_json(response),
                request_id=response.headers.get("X-Request-Id"),
                retry_after=response.headers.get("Retry-After"),
            )
        return _safe_json(response)

    # --- Reads -------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """The mock's health endpoint. Unprotected, so it works without a key.

        Uses the raw client rather than `_request` so a health probe is never retried or
        turned into a `TicketingError`: its only job is to answer "is it up".
        """
        try:
            response = await self._http.get("/health")
        except httpx.HTTPError as exc:
            raise TransportError(f"health check failed: {exc}") from exc
        if response.status_code != 200:
            raise error_from_response(response.status_code, _safe_json(response))
        return _require_object(_safe_json(response))

    async def search(self, query: SearchQuery) -> SearchResult:
        """One page of search results, with ``total`` and ``has_more`` preserved."""
        payload = await self._request("GET", "/tickets", params=query.as_query_params())
        return _parse(SearchResult, payload)

    async def get_ticket(self, ticket_id: str) -> Ticket:
        """One ticket, including its comments.

        The id is upper-cased here because Stage 1 showed the mock does it anyway; doing
        it client-side keeps the round trip identical for ``tck-1001`` and ``TCK-1001``.
        """
        payload = await self._request("GET", f"/tickets/{ticket_id.upper()}")
        return _parse(Ticket, payload)

    # --- Writes, as raw transport -----------------------------------------
    #
    # These perform the request and nothing else. The idempotency decision, and the
    # read-back that resolves a timeout, belong to `writes.py`, which wraps these. The
    # split exists so that the transport can be exercised on its own — including against
    # a fault-injecting mock — without a ledger in the way.

    async def create_ticket_raw(self, ticket: NewTicket) -> Ticket:
        """``POST /tickets``. Prefer `writes.IdempotentWriter.create_ticket`."""
        payload = await self._request("POST", "/tickets", json=ticket.model_dump(exclude_none=True))
        return _parse(Ticket, payload)

    async def add_comment_raw(self, ticket_id: str, comment: NewComment) -> Comment:
        """``POST /tickets/{id}/comments``. Prefer the guarded writer."""
        payload = await self._request(
            "POST",
            f"/tickets/{ticket_id.upper()}/comments",
            json=comment.model_dump(exclude_none=True),
        )
        return _parse(Comment, payload)


def _safe_json(response: httpx.Response) -> Any:
    """Decode a response body without ever raising.

    An error response with a non-JSON body is exactly the situation where raising a
    second, more confusing exception would be least helpful. A ``{"detail": ...}``
    fallback keeps the message useful and lets ``errors`` classify by status instead.
    """
    try:
        return response.json()
    except ValueError:
        text = response.text.strip()
        return {"detail": text} if text else None


def _require_object(payload: Any) -> dict[str, Any]:
    """Assert a payload is a JSON object, for endpoints expected to return one."""
    if not isinstance(payload, dict):
        raise MalformedResponseError(
            f"expected a JSON object from the API, got {type(payload).__name__}"
        )
    return payload


def _parse[T: BaseModel](model: type[T], payload: Any) -> T:
    """Validate a payload against a model, reporting the failure in our vocabulary.

    The `BaseModel` bound is what makes this type-check without a suppression: pydantic
    types `model_validate` as returning `Self`, so a `type[T]` where `T` is a model
    yields `T` and the return type is a checked claim rather than an `Any`.

    Pydantic's own ``ValidationError`` would leak the model's internals into the MCP
    tool result. Converting it here keeps one error vocabulary for callers, and makes a
    contract drift — the mock changing shape under us — a clear `MalformedResponseError`
    rather than a stack trace from a validation library.
    """
    try:
        return model.model_validate(payload)
    except PydanticValidationError as exc:
        raise MalformedResponseError(
            f"the API returned a body that does not match "
            f"{getattr(model, '__name__', model)}: {exc.error_count()} problem(s)"
        ) from exc
