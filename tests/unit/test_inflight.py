"""The in-flight run registry — what makes a cancel request able to reach a running turn.

Small, but every property here is load-bearing. The registry is the only thing standing between
an HTTP request and a coroutine somebody wants stopped, and each of the tests below pins a way it
could be wrong that would not show up as a crash: a cancel that reaches nothing, a cancel that
reaches somebody else's run, or a token that vanishes while the run it names is still stopping.
"""

from __future__ import annotations

from agent_service.agent.inflight import InflightRegistry


def test_a_started_run_is_registered_and_counted() -> None:
    registry = InflightRegistry()

    run = registry.start(session_id="sess-1", user_id="alice")

    assert run.run_id in registry
    assert registry.count == 1
    assert run.session_id == "sess-1"
    assert run.user_id == "alice"


def test_each_run_gets_its_own_id_and_token() -> None:
    """Two concurrent runs must not share a stop flag.

    Sharing one would mean cancelling either cancels both, and the user who cancelled one would
    have silently stopped the other.
    """
    registry = InflightRegistry()

    first = registry.start(session_id="sess-1", user_id="alice")
    second = registry.start(session_id="sess-2", user_id="alice")

    assert first.run_id != second.run_id
    assert first.token is not second.token


def test_run_ids_are_minted_here_not_supplied() -> None:
    """So a client cannot name a run it does not own.

    The id carries a recognisable prefix, which makes it obvious at a glance whether a value
    came from this registry or from a caller.
    """
    registry = InflightRegistry()

    run = registry.start(session_id="sess-1", user_id="alice")

    assert run.run_id.startswith("run-")


def test_finishing_a_run_forgets_it() -> None:
    registry = InflightRegistry()
    run = registry.start(session_id="sess-1", user_id="alice")

    registry.finish(run.run_id)

    assert run.run_id not in registry
    assert registry.count == 0


def test_finishing_a_run_twice_is_not_an_error() -> None:
    """A run that ended twice — a retry, a double cleanup — must not raise."""
    registry = InflightRegistry()
    run = registry.start(session_id="sess-1", user_id="alice")

    registry.finish(run.run_id)
    registry.finish(run.run_id)

    assert registry.count == 0


# --- cancelling ---------------------------------------------------------------


def test_cancelling_a_run_sets_the_token_the_loop_reads() -> None:
    """The whole point: the flag the run checks between steps is the one set here."""
    registry = InflightRegistry()
    run = registry.start(session_id="sess-1", user_id="alice")

    assert registry.cancel(run.run_id, user_id="alice") is True

    assert run.token.cancelled, "the run would never notice a cancel that missed its token"


def test_cancelling_an_unknown_run_reports_that_it_was_not_found() -> None:
    """Answered rather than raised, so the endpoint can return 404 rather than 500."""
    registry = InflightRegistry()

    assert registry.cancel("run-does-not-exist", user_id="alice") is False


def test_one_user_cannot_cancel_another_users_run() -> None:
    """Reported exactly as a missing run, so the endpoint cannot enumerate someone else's.

    Returning a distinct "not yours" answer would confirm the id exists, which is information
    about another user's activity that the caller is not entitled to.
    """
    registry = InflightRegistry()
    run = registry.start(session_id="sess-1", user_id="alice")

    assert registry.cancel(run.run_id, user_id="mallory") is False
    assert run.token.cancelled is False, "another user's cancel must not stop this run"


def test_a_cancelled_run_stays_registered_until_it_finishes() -> None:
    """A second cancel must still find it.

    Removing the token on cancel would answer a repeated click — or a client retrying a timeout
    — with "no such run" for a run that is in fact still stopping, which reads as a failure when
    the cancel worked.
    """
    registry = InflightRegistry()
    run = registry.start(session_id="sess-1", user_id="alice")

    registry.cancel(run.run_id, user_id="alice")

    assert run.run_id in registry
    assert registry.cancel(run.run_id, user_id="alice") is True, "a second cancel is not a miss"


def test_cancelling_one_run_leaves_the_others_alone() -> None:
    registry = InflightRegistry()
    first = registry.start(session_id="sess-1", user_id="alice")
    second = registry.start(session_id="sess-2", user_id="alice")

    registry.cancel(first.run_id, user_id="alice")

    assert first.token.cancelled
    assert second.token.cancelled is False


def test_cancelling_carries_a_reason_for_the_trace() -> None:
    """A cancelled run reports why, and the reason comes from here rather than the run."""
    registry = InflightRegistry()
    run = registry.start(session_id="sess-1", user_id="alice")

    registry.cancel(run.run_id, user_id="alice")

    assert run.token.reason, "the token must say why, not just that it happened"
    assert "cancel" in run.token.reason.lower()
