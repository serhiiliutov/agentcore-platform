"""Unit tests for the MCP client's error taxonomy.

Hermetic: no mock, no HTTP, no Docker. These assert the mapping from an HTTP response to
an exception, and the retry decision that follows from it.

The tests matter because the taxonomy is where Stage 1's two load-bearing findings become
code, and a wrong mapping is invisible until it matters:

* If the parser reads only ``error``, a missing ticket arrives as an unexplained failure
  instead of a 404, and the agent tells the user something went wrong rather than that
  the ticket does not exist.
* If ``RequestTimeoutError`` were retryable, a slow write could become two tickets. The
  mock has no idempotency, so nothing downstream would catch it.

Both are asserted directly rather than inferred from the implementation.
"""

from __future__ import annotations

import pytest

from ticketing_mcp.errors import (
    AuthenticationError,
    NotFoundError,
    RateLimitedError,
    RequestTimeoutError,
    ServerError,
    TicketingError,
    ValidationError,
    error_from_response,
    is_retryable,
    is_terminal,
)

# --- The mock's own middleware shape: {"error": ..., "detail": ...} ---------


def test_unauthorized_maps_to_authentication_error() -> None:
    error = error_from_response(
        401, {"error": "unauthorized", "detail": "missing or invalid X-API-Key"}
    )

    assert isinstance(error, AuthenticationError)
    assert error.code == "unauthorized"
    assert error.status == 401


def test_rate_limited_maps_to_rate_limited_error() -> None:
    error = error_from_response(429, {"error": "rate_limited", "detail": "too many requests"})

    assert isinstance(error, RateLimitedError)
    assert error.status == 429


def test_server_error_maps_to_server_error() -> None:
    error = error_from_response(500, {"error": "internal_error", "detail": "boom"})

    assert isinstance(error, ServerError)
    assert error.status == 500


# --- FastAPI's own shape: {"detail": ...} with no "error" key ---------------


def test_a_404_with_no_error_key_still_maps_to_not_found() -> None:
    """The regression this test exists for.

    The mock delegates 404 to FastAPI, which sends ``{"detail": ...}`` alone. A parser
    that requires an ``error`` key would produce a generic error here, and the agent
    would be unable to say "there is no such ticket".
    """
    error = error_from_response(404, {"detail": "ticket TCK-9999 not found"})

    assert isinstance(error, NotFoundError)
    assert error.status == 404
    assert "TCK-9999" in (error.detail or "")


def test_a_400_with_no_error_key_maps_to_validation_error() -> None:
    error = error_from_response(400, {"detail": "nothing to update"})

    assert isinstance(error, ValidationError)
    assert error.detail == "nothing to update"


def test_a_422_with_no_error_key_maps_to_validation_error() -> None:
    error = error_from_response(422, {"detail": "invalid priority"})

    assert isinstance(error, ValidationError)
    assert error.status == 422


def test_a_422_validation_list_is_summarised_to_field_names() -> None:
    """FastAPI's 422 sends a list. A user needs the field, not the whole structure."""
    body = {
        "detail": [
            {"loc": ["body", "title"], "msg": "too short", "type": "string_too_short"},
            {"loc": ["body", "requester"], "msg": "required", "type": "missing"},
        ]
    }

    error = error_from_response(422, body)

    assert isinstance(error, ValidationError)
    assert error.detail is not None
    assert "title" in error.detail
    assert "requester" in error.detail


# --- Degenerate bodies must not raise ---------------------------------------


@pytest.mark.parametrize("body", [None, "not json", 42, [], {}, {"detail": 123}])
def test_an_unrecognised_body_never_raises(body: object) -> None:
    """An error path that itself throws turns a confusing backend into a crash."""
    error = error_from_response(418, body)

    assert isinstance(error, TicketingError)


def test_an_unknown_status_is_generic_rather_than_misclassified() -> None:
    error = error_from_response(302, {"detail": "moved"})

    assert type(error) is TicketingError
    assert error.status == 302


# --- Retry eligibility: the decision that prevents double writes ------------


@pytest.mark.parametrize(
    "error",
    [
        RateLimitedError("throttled"),
        ServerError("boom"),
    ],
)
def test_pre_handler_failures_are_retryable(error: TicketingError) -> None:
    """Stage 1 proved chaos is injected before routing, so these wrote nothing."""
    assert is_retryable(error)


@pytest.mark.parametrize(
    "error",
    [
        AuthenticationError("bad key"),
        NotFoundError("no such ticket"),
        ValidationError("bad request"),
    ],
)
def test_terminal_failures_are_not_retryable(error: TicketingError) -> None:
    assert not is_retryable(error)
    assert is_terminal(error)


def test_a_timeout_is_never_retryable() -> None:
    """The single most important assertion in this file.

    A timeout is compatible with the write having succeeded: the client gave up while
    the mock was sleeping in its latency knob. Retrying it can create a second ticket,
    and Stage 1 proved the mock will happily make two.
    """
    error = RequestTimeoutError("gave up waiting")

    assert not is_retryable(error)
    assert not is_terminal(error), "a timeout is resolved by verification, not by giving up"


def test_a_timeout_is_not_retryable_even_when_it_carries_a_status() -> None:
    assert not is_retryable(RequestTimeoutError("gave up", status=504))


# --- Retry-After parsing ----------------------------------------------------


def test_retry_after_seconds_are_parsed() -> None:
    error = error_from_response(429, {"error": "rate_limited"}, retry_after="1")

    assert isinstance(error, RateLimitedError)
    assert error.retry_after_s == 1.0


def test_a_missing_retry_after_is_none_not_a_guess() -> None:
    """None lets the policy apply its own backoff, which is the correct behaviour."""
    error = error_from_response(429, {"error": "rate_limited"})

    assert isinstance(error, RateLimitedError)
    assert error.retry_after_s is None


@pytest.mark.parametrize("value", ["", "not-a-number", "-5", "Wed, 21 Oct 2015 07:28:00 GMT"])
def test_an_unusable_retry_after_is_ignored(value: str) -> None:
    """The HTTP-date form is legal but never sent by the mock, and guessing is worse."""
    error = error_from_response(429, {"error": "rate_limited"}, retry_after=value)

    assert isinstance(error, RateLimitedError)
    assert error.retry_after_s is None


# --- Observability of the error itself --------------------------------------


def test_the_request_id_is_carried_through_for_correlation() -> None:
    error = error_from_response(500, {"error": "internal_error"}, request_id="req-abc-123")

    assert error.request_id == "req-abc-123"
    assert "req-abc-123" in str(error)


def test_as_dict_reports_retryability() -> None:
    throttled = error_from_response(429, {"error": "rate_limited"})
    missing = error_from_response(404, {"detail": "nope"})

    assert throttled.as_dict()["retryable"] is True
    assert missing.as_dict()["retryable"] is False


def test_as_dict_never_contains_the_request_body() -> None:
    """A ticket body can contain anything a user typed; an error log is not the place."""
    rendered = error_from_response(422, {"detail": "title too short"}).as_dict()

    assert set(rendered) == {
        "kind",
        "message",
        "status",
        "code",
        "detail",
        "request_id",
        "retryable",
    }
