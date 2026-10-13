"""Retry policy, and the one case it deliberately refuses to handle.

Read this alongside `errors.py`. Stage 1 established the fact this module is built on:

    the mock injects chaos *before* the route handler runs.

So a ``429`` or a ``500`` proves the write did not happen, and retrying is free. This
module exploits exactly that, and nothing more.

**What it will not do: retry a timeout.** A timeout means the client gave up while the
mock was sleeping in its latency knob. The request may have been received and completed
afterwards. Retrying it is a coin flip that can create a second ticket, and the mock has
no idempotency of its own to catch that — Stage 1 proved it creates two. So
``RequestTimeoutError`` is not in the retryable set, and this module has no flag to put
it there. Recovering from a timeout is a *verification* problem, not a retry problem,
and it is solved in ``client.py`` by reading back.

Backoff is exponential with full jitter, except when the server states a delay: a ``429``
carrying ``Retry-After`` is honoured exactly, because retrying sooner than the server
asked is how a client earns a longer ban. Jitter exists so that several callers throttled
by the same event do not retry in lockstep and reproduce the burst that caused it.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .errors import RateLimitedError, RequestTimeoutError, TicketingError, is_retryable

#: The default sleeps between attempts. Overridden in tests to keep them fast.
SleepFn = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to retry, and how long to wait between attempts."""

    #: Total attempts, not retries. 1 means "never retry".
    max_attempts: int = 4
    #: Delay before the second attempt; doubles each time thereafter.
    base_delay_s: float = 0.25
    #: Ceiling, so a longer outage does not produce an absurd wait.
    max_delay_s: float = 8.0
    #: When the server sends ``Retry-After``, use it instead of the computed backoff.
    respect_retry_after: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(
                "max_attempts must be at least 1: a policy with no attempts cannot call anything"
            )
        if self.base_delay_s < 0 or self.max_delay_s < 0:
            raise ValueError("delays cannot be negative")

    def delay_for_attempt(self, attempt: int, *, retry_after_s: float | None = None) -> float:
        """Seconds to wait before attempt number ``attempt + 1``.

        ``attempt`` is 1-based: 1 is the first attempt, whose delay is meaningless
        because nothing has failed yet. Attempt 2 waits ``base_delay_s``, attempt 3
        waits twice that, and so on up to ``max_delay_s``.
        """
        if retry_after_s is not None and self.respect_retry_after:
            # The server told us when to come back. Coming back earlier is a way to
            # be told to wait longer. Still capped, so a hostile or buggy value cannot
            # stall a request past its deadline.
            return min(retry_after_s, self.max_delay_s)

        exponential = self.base_delay_s * (2 ** (attempt - 1))
        capped = min(exponential, self.max_delay_s)
        # Full jitter: anywhere from 0 to the capped value. Uniform rather than
        # "capped minus a bit", so a fleet of callers spreads out instead of bunching.
        return random.uniform(0, capped)


async def call_with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    sleep: SleepFn | None = None,
    on_attempt_failed: Callable[[int, BaseException], None] | None = None,
) -> T:
    """Run ``operation`` until it succeeds or the policy is exhausted.

    ``operation`` is a zero-argument callable returning a fresh awaitable. It is a
    callable rather than a coroutine so that each attempt is a *new* request: re-awaiting
    one coroutine is a runtime error, and a retry that silently reuses a consumed
    response would be worse than no retry at all.

    A ``RequestTimeoutError`` propagates immediately and is never retried. The caller
    decides what to do about it — for a write, that means verifying; for a read, it is
    usually just an error. This function refuses to guess.

    ``on_attempt_failed`` is called before each sleep, so a caller can log the attempt
    count without this module knowing about logging.
    """
    resolved = policy or RetryPolicy()
    resolved_sleep = sleep or asyncio.sleep

    last_error: BaseException | None = None

    for attempt in range(1, resolved.max_attempts + 1):
        try:
            return await operation()
        except RequestTimeoutError:
            # Never retried, regardless of remaining attempts. See the module docstring.
            raise
        except BaseException as error:
            if not is_retryable(error):
                raise
            last_error = error

            if attempt >= resolved.max_attempts:
                break

            if on_attempt_failed is not None:
                on_attempt_failed(attempt, error)

            retry_after = error.retry_after_s if isinstance(error, RateLimitedError) else None
            await resolved_sleep(resolved.delay_for_attempt(attempt, retry_after_s=retry_after))

    # The loop can only exit here after exhausting attempts, so last_error is set.
    assert last_error is not None, "retry loop exited without recording a failure"
    # The attempt count is recorded on the way out so the failure can describe how hard the
    # client already tried. Without it the re-raised error carries the message a single
    # attempt would have produced - "the API is rate limiting this client" - which reads like
    # something that just happened, and invites the reader to retry by hand at exactly the
    # moment the client has decided retrying is not working.
    if isinstance(last_error, TicketingError):
        last_error.attempts = resolved.max_attempts
    raise last_error
