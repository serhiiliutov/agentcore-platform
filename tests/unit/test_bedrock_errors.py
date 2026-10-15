"""Bedrock's failure shapes, translated into the status the loop's hint table acts on.

boto3 raised ``ClientError`` and never set ``.status_code``, so before this translation every Bedrock
failure reached the loop with no status and got the generic hint — "check the credential" — including
a throttling or quota error, which is precisely the case whose advice has to be specific. These tests
pin the mapping and, just as importantly, the two cases where it deliberately does nothing.

Hermetic: no AWS credential, no region, no network. The provider is built with ``__new__`` so the
boto3 client is never constructed, and its transport is replaced with one that fails the way boto3
fails — by raising, with the reason in ``response["Error"]["Code"]``.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_service.providers.base import Message
from agent_service.providers.bedrock_provider import (
    BedrockProvider,
    _as_http_shaped,
    _error_code,
)


class _BotocoreError(RuntimeError):
    """A stand-in shaped exactly like botocore's ``ClientError``.

    The shape is the whole point: boto3 reports the reason in a nested ``response`` mapping rather than
    in an HTTP status, which is why the translation exists at all. It is a real subclass rather than a
    ``RuntimeError`` with an attribute bolted on, so the stand-in is typed and reads as the thing it
    models.
    """

    def __init__(self, code: str) -> None:
        super().__init__(f"An error occurred ({code}) when calling the Converse operation")
        self.response: dict[str, Any] = {"Error": {"Code": code, "Message": code}}


def _client_error(code: str) -> _BotocoreError:
    """One of the stand-ins, by error code."""
    return _BotocoreError(code)


class _RaisingClient:
    """A bedrock-runtime client that always fails, so the provider's error path is exercised."""

    def __init__(self, code: str) -> None:
        self._code = code

    def converse(self, **kwargs: Any) -> Any:
        raise _client_error(self._code)


def _provider_failing_with(code: str) -> BedrockProvider:
    """A provider whose transport raises ``code``, built without boto3 or a region."""
    provider = BedrockProvider.__new__(BedrockProvider)
    provider._client = _RaisingClient(code)
    provider._model = "a-model-id"
    provider._name = "bedrock"
    return provider


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("ThrottlingException", 429),
        ("ThrottlingError", 429),
        ("TooManyRequestsException", 429),
        ("ServiceQuotaExceededException", 429),
        ("AccessDeniedException", 403),
        ("UnauthorizedException", 401),
        ("ResourceNotFoundException", 404),
        ("ValidationException", 400),
    ],
)
def test_a_recognised_bedrock_code_becomes_the_status_the_loop_knows(code: str, status: int) -> None:
    """Each of these produces different advice, so each has to arrive distinguishably."""
    assert getattr(_as_http_shaped(_client_error(code)), "status_code", None) == status


def test_an_unrecognised_code_is_left_exactly_as_it_was() -> None:
    """The generic hint is the honest answer for a failure we have no specific advice for."""
    original = _client_error("SomethingBrandNewException")

    assert _as_http_shaped(original) is original
    assert not hasattr(original, "status_code")


def test_an_exception_with_no_botocore_shape_is_left_alone() -> None:
    """A mocked or proxied client may raise something that is not a ClientError at all."""
    original = RuntimeError("connection reset by peer")

    assert _as_http_shaped(original) is original


def test_the_error_code_is_read_defensively() -> None:
    """An error path must not itself fail, whatever it is handed."""
    assert _error_code(_client_error("ThrottlingException")) == "ThrottlingException"
    assert _error_code(RuntimeError("no response attribute")) == ""

    class NotAMappingError(Exception):
        """``response`` exists but is the wrong shape entirely."""

        def __init__(self) -> None:
            super().__init__("response is a string, not a mapping")
            self.response = "a string where a mapping should be"

    assert _error_code(NotAMappingError()) == ""

    class NoErrorKeyError(Exception):
        """``response`` is a mapping but carries no ``Error`` entry."""

        def __init__(self) -> None:
            super().__init__("the response carries no Error key")
            self.response: dict[str, Any] = {"ResponseMetadata": {"HTTPStatusCode": 400}}

    assert _error_code(NoErrorKeyError()) == ""


async def test_a_throttled_turn_surfaces_as_a_throttled_turn() -> None:
    """The end of the chain: what a caller sees when Bedrock says "no quota left"."""
    provider = _provider_failing_with("ThrottlingException")

    with pytest.raises(RuntimeError) as raised:
        await provider.chat([Message(role="user", content="anything")])

    # The status is what the loop's hint table reads.
    assert getattr(raised.value, "status_code", None) == 429
    # And the original error type survives, so the loop's log names what actually happened rather than
    # a type this module invented.
    assert type(raised.value) is _BotocoreError


async def test_an_unmapped_turn_still_fails_with_the_real_reason() -> None:
    """Translation must never swallow a failure — only classify the ones it recognises."""
    provider = _provider_failing_with("SomeFutureException")

    with pytest.raises(RuntimeError, match="SomeFutureException"):
        await provider.chat([Message(role="user", content="anything")])
