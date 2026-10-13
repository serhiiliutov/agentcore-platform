"""Fault injection: the mock's ``FLAKY`` mode, pinned.

Everything about the retry policy in Stage 4 rests on one distinction that only this
file can establish:

* A ``429`` or ``500`` from the mock is raised by middleware **before the route
  handler runs**. So a write that fails that way definitely did not happen, and
  retrying it is safe.
* A **timeout** is not the same thing. The mock's middleware sleeps ``LATENCY_MS``
  *before* deciding, so a client that gives up during that sleep has no idea whether
  the handler then executed. Retrying blindly could create a second ticket.

That asymmetry — failures are safe to retry, silence is not — is why the MCP server
needs both a retry policy and an idempotency ledger, rather than just one of them.
These tests keep whoever reads that code later from "simplifying" it into a plain
retry loop.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from .conftest import JsonObject, MockContract

#: Statuses the chaos middleware can produce.
_CHAOS_STATUSES = {429, 500}

#: Enough requests that a seeded 50/50 split exercises both branches.
_SAMPLE_SIZE = 30

_VALID_TICKET: dict[str, str] = {
    "title": "Chaos created ticket",
    "requester": "Priya Raman",
}


def test_a_flaky_read_never_succeeds(
    flaky_client: TestClient, mock: MockContract
) -> None:
    response = flaky_client.get("/tickets", headers=mock.auth_headers())

    assert response.status_code in _CHAOS_STATUSES


def test_a_flaky_failure_says_which_kind_it_is(
    flaky_client: TestClient, mock: MockContract
) -> None:
    """Both shapes are model-readable: a code and a sentence."""
    body = flaky_client.get("/tickets", headers=mock.auth_headers()).json()

    assert body["error"] in {"rate_limited", "internal_error"}
    assert body["detail"]


def test_the_rate_limited_response_carries_retry_after(
    flaky_client: TestClient, mock: MockContract
) -> None:
    """``Retry-After`` is what lets the retry loop wait the right amount.

    Retrying a 429 immediately is the classic way to be throttled harder, so the
    policy in Stage 4 reads this header rather than inventing a delay.
    """
    responses = [
        flaky_client.get("/tickets", headers=mock.auth_headers())
        for _ in range(_SAMPLE_SIZE)
    ]
    throttled = [r for r in responses if r.status_code == 429]

    assert throttled, "the seeded sequence should include a 429"
    for response in throttled:
        assert response.headers["Retry-After"] == "1"
        assert response.json()["error"] == "rate_limited"


def test_the_seeded_sequence_produces_both_failure_modes(
    flaky_client: TestClient, mock: MockContract
) -> None:
    """The suite must exercise 429 *and* 500, not just whichever comes first."""
    statuses = {
        flaky_client.get("/tickets", headers=mock.auth_headers()).status_code
        for _ in range(_SAMPLE_SIZE)
    }

    assert statuses == _CHAOS_STATUSES


def test_the_sequence_is_deterministic_for_a_given_seed(
    load_app: Callable[..., FastAPI], mock: MockContract
) -> None:
    """Two independently loaded mocks with the same seed fail identically.

    Without this, chaos would be indistinguishable from flakiness in the *test
    suite*, and a failure-handling test that sometimes passes proves nothing.
    """
    sequences: list[list[int]] = []
    for _ in range(2):
        client = TestClient(load_app(FLAKY="1", FLAKY_RATE="1.0", FLAKY_SEED="7"))
        sequences.append(
            [
                client.get("/tickets", headers=mock.auth_headers()).status_code
                for _ in range(10)
            ]
        )

    assert sequences[0] == sequences[1]
    assert set(sequences[0]) <= _CHAOS_STATUSES


def test_health_is_never_affected_by_chaos(
    flaky_client: TestClient, mock: MockContract
) -> None:
    """Health is unprotected, so it stays a usable liveness signal.

    This is what makes the compose health check trustworthy while the mock is in a
    failing mode.
    """
    response = flaky_client.get("/health")

    assert response.status_code == 200
    assert response.json()["flaky"] is True


def test_a_bad_key_is_still_a_401_not_a_chaos_failure(
    flaky_client: TestClient, mock: MockContract
) -> None:
    """Authentication is checked *before* the dice are rolled.

    So a credential problem is never reported as a transient backend problem, which
    would otherwise be retried pointlessly.
    """
    response = flaky_client.get("/tickets", headers=mock.auth_headers("wrong"))

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_a_rejected_write_does_not_reach_the_handler(
    flaky_client: TestClient, mock: MockContract
) -> None:
    """The core retry-safety guarantee.

    Chaos is decided before routing, so a failed create never created anything. A
    retry after one of these failures cannot duplicate a ticket — which is exactly
    why the retry policy treats 429/500 differently from a timeout.
    """
    before = flaky_client.get("/health").json()["tickets"]

    response = flaky_client.post(
        "/tickets", headers=mock.auth_headers(), json=_VALID_TICKET
    )

    assert response.status_code in _CHAOS_STATUSES
    assert flaky_client.get("/health").json()["tickets"] == before


def test_a_failure_body_is_json_not_html(
    flaky_client: TestClient, mock: MockContract
) -> None:
    """A model is parsing this, so it must never be an HTML error page."""
    response = flaky_client.get("/tickets", headers=mock.auth_headers())
    body: JsonObject = response.json()

    assert isinstance(body, dict)
    assert set(body) >= {"error", "detail"}


def test_latency_alone_does_not_cause_failure(
    load_app: Callable[..., FastAPI], mock: MockContract
) -> None:
    """Latency is a separate knob from chaos, and does not fail a request.

    A slow-but-correct response is exactly why the timeout path needs care: the
    request may well have succeeded, which is the case a blind retry cannot resolve
    on its own.
    """
    client = TestClient(load_app(FLAKY="0", LATENCY_MS="1"))

    assert client.get("/tickets", headers=mock.auth_headers()).status_code == 200
