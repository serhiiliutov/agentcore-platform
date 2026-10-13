"""The error taxonomy, derived from the mock's observed behaviour rather than guessed.

Every class here corresponds to something `tests/contract/` proved. The mapping from
HTTP response to exception is not a taste decision; it is a reading of the fixture.

**Two body shapes, not one.** Stage 1 found that the mock answers its own middleware
errors (401, 429, 500) with ``{"error", "detail"}`` while FastAPI answers its routing
and validation errors (400, 404, 422) with ``{"detail"}`` alone. A parser that reads
only ``error`` reports a missing ticket as an unexplained failure, so
:func:`error_from_response` reads both and never assumes either.

**Retryable is a property of the failure, not of the code that catches it.** Stage 1's
second finding is the load-bearing one: the mock injects chaos *before* the route
handler runs, so a 429 or 500 provably wrote nothing and retrying is safe. A timeout is
not the same thing — the client gave up while the mock was sleeping, and the handler may
have run to completion afterwards. So:

* ``RateLimitedError`` and ``ServerError`` are safe to retry: nothing happened.
* ``RequestTimeoutError`` is **not** safe to retry on its own: something may have
  happened, and nothing in the response says which. It is resolved by reading back,
  which is why the client has verification and not just a retry loop.
* ``AuthenticationError``, ``NotFoundError``, ``ValidationError`` and
  ``MalformedResponseError`` are never retried: the same request will fail the same way,
  and retrying only delays the error the user needs to see.

:func:`is_retryable` is the single place that decision is made, so the rule exists once
rather than at every ``except`` clause.
"""

from __future__ import annotations

from typing import Any, Final

#: Error codes the mock's own middleware uses, as observed in the contract suite.
CODE_UNAUTHORIZED: Final = "unauthorized"
CODE_RATE_LIMITED: Final = "rate_limited"
CODE_INTERNAL_ERROR: Final = "internal_error"


class TicketingError(Exception):
    """Base class for every failure this client raises.

    Carries the pieces a caller needs to explain the failure to a user without
    re-parsing anything: an HTTP status when there was one, the backend's own error
    code when it sent one, and the correlation id so a log line can be tied to the
    mock's view of the same request.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        detail: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.detail = detail
        self.request_id = request_id

    def __str__(self) -> str:
        parts = [self.message]
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.code:
            parts.append(f"code={self.code}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        """A model-readable and log-safe rendering.

        Deliberately excludes the request body: a ticket body can contain anything a
        user typed, and an error path is not the place to copy it into a log.
        """
        return {
            "kind": type(self).__name__,
            "message": self.message,
            "status": self.status,
            "code": self.code,
            "detail": self.detail,
            "request_id": self.request_id,
            "retryable": is_retryable(self),
        }


class ConfigurationError(TicketingError):
    """The client is not usable: no base URL, no API key.

    A programming or deployment fault, not a backend one. Raised at construction.
    """


class AuthenticationError(TicketingError):
    """``401`` — the credential was rejected.

    Never transient. Stage 1 established that authentication is checked *before* the
    mock rolls its chaos dice, so this can only mean the key is wrong or missing, and
    retrying it would hammer a door that will not open.
    """


class NotFoundError(TicketingError):
    """``404`` — no such ticket."""


class ValidationError(TicketingError):
    """``400`` or ``422`` — the backend refused the request itself.

    Covers both the mock's own ``400 {"detail": "nothing to update"}`` and FastAPI's
    ``422`` validation body. Either way the request is wrong and will stay wrong.
    """


class RateLimitedError(TicketingError):
    """``429`` — throttled. Nothing was written; safe to retry.

    ``retry_after_s`` is read from the response's ``Retry-After`` header, which Stage 1
    confirmed the mock always sends (``1``). Inventing a backoff when the server states
    one is how clients end up throttled harder.
    """

    def __init__(self, message: str, *, retry_after_s: float | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after_s = retry_after_s


class ServerError(TicketingError):
    """``500`` — the backend failed before handling the request. Safe to retry."""


class TransportError(TicketingError):
    """The connection failed before a response arrived.

    Ambiguous by nature: a refusal means nothing happened, but a reset mid-response
    could mean the request was received. Treated as retryable because a read-back
    verification follows a write regardless — see ``client.verify_*``.
    """


class RequestTimeoutError(TicketingError):
    """The client gave up waiting.

    **Not** the same as a failure. Stage 1's chaos middleware sleeps *before* it
    decides whether to fail, so a timeout is compatible with the request having
    succeeded. This class exists to make that distinction unmissable: code that retries
    it blindly can create a duplicate ticket, which is the exact bug the mock's lack of
    idempotency makes possible.
    """


class MalformedResponseError(TicketingError):
    """A response arrived but could not be understood.

    A successful status with a body that is not the expected JSON is a contract
    violation, and retrying it will produce the same violation.
    """


#: Failures where the request provably did not take effect, and retrying is safe.
_RETRYABLE: Final = (RateLimitedError, ServerError, TransportError)

#: Failures where retrying cannot help and only delays the real error.
_TERMINAL: Final = (
    ConfigurationError,
    AuthenticationError,
    NotFoundError,
    ValidationError,
    MalformedResponseError,
)


def is_retryable(error: BaseException) -> bool:
    """Whether ``error`` may be retried without the risk of a double write.

    ``RequestTimeoutError`` is deliberately in neither tuple: the decision depends on
    whether the call was a read or a write, which this function cannot know. Callers
    handle it explicitly, and the default is to *not* treat it as retryable.
    """
    return isinstance(error, _RETRYABLE)


def is_terminal(error: BaseException) -> bool:
    """Whether no amount of waiting or repetition will change this outcome."""
    return isinstance(error, _TERMINAL)


def _extract(body: object) -> tuple[str | None, str | None]:
    """Pull ``(code, detail)`` out of either body shape.

    Returns ``(None, None)`` for anything unrecognised rather than raising: an error
    path must not itself fail, or a confusing backend becomes an unexplained crash.
    """
    if not isinstance(body, dict):
        return None, None

    code = body.get("error")
    detail = body.get("detail")

    # The mock's own shape: {"error": "...", "detail": "..."}.
    if isinstance(code, str):
        return code, detail if isinstance(detail, str) else None

    # FastAPI's shape: {"detail": "..."} or {"detail": [ {...}, ... ]}.
    if isinstance(detail, str):
        return None, detail
    if isinstance(detail, list):
        # A validation error lists one entry per offending field. Summarise to the
        # field names, which is what a user can act on.
        fields = [str(item.get("loc", ["?"])[-1]) for item in detail if isinstance(item, dict)]
        return None, f"invalid fields: {', '.join(fields)}" if fields else "invalid request"

    return None, None


def error_from_response(
    status: int,
    body: object,
    *,
    request_id: str | None = None,
    retry_after: str | None = None,
) -> TicketingError:
    """Map an HTTP response to the right exception.

    The single translation point between the mock's wire behaviour and this client's
    vocabulary. Every status the contract suite observed has an explicit branch; an
    unrecognised status becomes a generic error rather than being silently treated as
    success or as a specific kind of failure.
    """
    code, detail = _extract(body)
    summary = detail or code or "the backend rejected the request"

    if status == 401:
        return AuthenticationError(
            "the ticketing API rejected the credential",
            status=status,
            code=code or CODE_UNAUTHORIZED,
            detail=detail,
            request_id=request_id,
        )
    if status == 404:
        return NotFoundError(
            summary, status=status, code=code, detail=detail, request_id=request_id
        )
    if status in {400, 422}:
        return ValidationError(
            summary, status=status, code=code, detail=detail, request_id=request_id
        )
    if status == 429:
        return RateLimitedError(
            "the ticketing API is rate limiting this client",
            status=status,
            code=code or CODE_RATE_LIMITED,
            detail=detail,
            request_id=request_id,
            retry_after_s=_parse_retry_after(retry_after),
        )
    if status >= 500:
        return ServerError(
            "the ticketing API failed while handling the request",
            status=status,
            code=code or CODE_INTERNAL_ERROR,
            detail=detail,
            request_id=request_id,
        )

    return TicketingError(summary, status=status, code=code, detail=detail, request_id=request_id)


def _parse_retry_after(value: str | None) -> float | None:
    """Read ``Retry-After`` as seconds.

    Only the delta-seconds form is honoured. The HTTP-date form is legal but the mock
    never sends it, and guessing at a date would be worse than ignoring it: an absent
    value lets the retry policy apply its own backoff, which is correct behaviour.
    """
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


__all__ = [
    "CODE_INTERNAL_ERROR",
    "CODE_RATE_LIMITED",
    "CODE_UNAUTHORIZED",
    "AuthenticationError",
    "ConfigurationError",
    "MalformedResponseError",
    "NotFoundError",
    "RateLimitedError",
    "RequestTimeoutError",
    "ServerError",
    "TicketingError",
    "TransportError",
    "ValidationError",
    "error_from_response",
    "is_retryable",
    "is_terminal",
]
