"""The memory repositories, and the handler that depends on them.

Two halves. Most of this is the repositories: ordering, scoping, expiry, and the update-in-place
rule that keeps one key to one answer. The last test is the one that matters most, because it is
the integration proof of a specific bug — a write proposed in a *fresh* conversation used to fail
on the approvals foreign key, and nothing here would have caught it before Stage 8 wrote it down.

Hermetic: in-memory SQLite with the same pragmas production uses, so a foreign-key violation is a
real failure here rather than something the test environment forgives.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import agent_service.db.models  # noqa: F401 - registers every table on Base.metadata
from agent_service.db.base import Base, create_session_factory, register_sqlite_pragmas
from agent_service.db.models.approval import ApprovalState
from agent_service.db.models.conversation import ChatSession
from agent_service.db.repositories import FactRepository, SessionRepository
from agent_service.db.repositories.sessions import UnknownRoleError, _role
from agent_service.providers.base import Message as ProviderMessage, ToolCallRequest

USER_ID = "user-1"
OTHER_USER = "user-2"
SESSION_ID = "session-1"


@pytest.fixture
def engine() -> Iterator[Engine]:
    built = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    register_sqlite_pragmas(built)
    Base.metadata.create_all(built)
    yield built
    built.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = create_session_factory(engine)
    with factory() as opened:
        yield opened


@pytest.fixture
def sessions(session: Session) -> SessionRepository:
    return SessionRepository(session)


@pytest.fixture
def facts(session: Session) -> FactRepository:
    return FactRepository(session)


# --- sessions: ensure ---------------------------------------------------------


def test_ensure_creates_the_session(sessions: SessionRepository, session: Session) -> None:
    """The row a proposed write's foreign key will point at."""
    created = sessions.ensure(SESSION_ID, user_id=USER_ID)

    assert created.id == SESSION_ID
    assert session.get(ChatSession, SESSION_ID) is not None


def test_ensure_is_idempotent(sessions: SessionRepository) -> None:
    """A second turn must not fail on a duplicate primary key."""
    first = sessions.ensure(SESSION_ID, user_id=USER_ID)
    second = sessions.ensure(SESSION_ID, user_id=USER_ID)

    assert first.id == second.id


def test_ensure_does_not_reassign_the_owner(sessions: SessionRepository) -> None:
    """A guessed session id must not become a way to take over someone's conversation.

    The session is returned rather than rejected, and the caller then finds nothing in it. What
    must NOT happen is the row being rewritten to the second caller's user id, which would hand
    them the first user's history.
    """
    sessions.ensure(SESSION_ID, user_id=USER_ID)
    again = sessions.ensure(SESSION_ID, user_id=OTHER_USER)

    assert again.user_id == USER_ID


# --- sessions: append and history ---------------------------------------------


def test_append_numbers_turns_from_one(sessions: SessionRepository) -> None:
    sessions.ensure(SESSION_ID, user_id=USER_ID)

    first = sessions.append(
        SESSION_ID, user_id=USER_ID, message=ProviderMessage(role="user", content="one")
    )
    second = sessions.append(
        SESSION_ID, user_id=USER_ID, message=ProviderMessage(role="assistant", content="two")
    )

    assert (first.sequence, second.sequence) == (1, 2)


def test_history_returns_turns_oldest_first(sessions: SessionRepository) -> None:
    """The order a provider must see. ``sequence`` decides it, not ``created_at``."""
    sessions.ensure(SESSION_ID, user_id=USER_ID)
    sessions.append_all(
        SESSION_ID,
        user_id=USER_ID,
        messages=[
            ProviderMessage(role="user", content="first"),
            ProviderMessage(role="assistant", content="second"),
            ProviderMessage(role="user", content="third"),
        ],
    )

    history = sessions.history(SESSION_ID, user_id=USER_ID)

    assert [m.content for m in history] == ["first", "second", "third"]


def test_history_keeps_the_newest_turns_when_bounded(sessions: SessionRepository) -> None:
    """A bound must drop the START of a conversation, not the part that matters.

    This is the test that would fail if the query selected ascending and sliced: it would
    return turns 1..2 and silently lose the current question.
    """
    sessions.ensure(SESSION_ID, user_id=USER_ID)
    sessions.append_all(
        SESSION_ID,
        user_id=USER_ID,
        messages=[ProviderMessage(role="user", content=f"turn-{n}") for n in range(1, 11)],
    )

    history = sessions.history(SESSION_ID, user_id=USER_ID, limit=3)

    assert [m.content for m in history] == ["turn-8", "turn-9", "turn-10"]


def test_history_is_scoped_by_user(sessions: SessionRepository) -> None:
    """The same session id read as another user yields nothing, not someone else's words."""
    sessions.ensure(SESSION_ID, user_id=USER_ID)
    sessions.append(
        SESSION_ID, user_id=USER_ID, message=ProviderMessage(role="user", content="private")
    )

    assert sessions.history(SESSION_ID, user_id=OTHER_USER) == ()


def test_history_round_trips_a_message(sessions: SessionRepository) -> None:
    sessions.ensure(SESSION_ID, user_id=USER_ID)
    sessions.append(
        SESSION_ID, user_id=USER_ID, message=ProviderMessage(role="user", content="round trip")
    )

    history = sessions.history(SESSION_ID, user_id=USER_ID)

    assert history[0].role == "user"
    assert history[0].content == "round trip"


def test_a_stored_tool_result_rebuilds_as_a_tool_result(sessions: SessionRepository) -> None:
    """A provider needs the call id to match a result to its call, so it survives the round trip."""
    sessions.ensure(SESSION_ID, user_id=USER_ID)
    call = ToolCallRequest(id="call-1", name="search_tickets", arguments={"query": "printer"})
    sessions.append_all(
        SESSION_ID,
        user_id=USER_ID,
        messages=[
            ProviderMessage.assistant("looking", (call,)),
            ProviderMessage.tool_result(call, "two tickets found"),
        ],
    )

    history = sessions.history(SESSION_ID, user_id=USER_ID)

    assert history[0].role == "assistant"
    assert history[0].tool_calls[0].id == "call-1"
    assert history[1].role == "tool"
    assert history[1].tool_call_id == "call-1"
    assert history[1].name == "search_tickets"
    assert history[1].content == "two tickets found"


def test_count_reports_stored_turns(sessions: SessionRepository) -> None:
    sessions.ensure(SESSION_ID, user_id=USER_ID)
    sessions.append_all(
        SESSION_ID,
        user_id=USER_ID,
        messages=[
            ProviderMessage(role="user", content="a"),
            ProviderMessage(role="assistant", content="b"),
        ],
    )

    assert sessions.count(SESSION_ID, user_id=USER_ID) == 2
    assert sessions.count(SESSION_ID, user_id=OTHER_USER) == 0


def test_an_unknown_stored_role_raises_rather_than_being_guessed() -> None:
    """Corruption is reported, not reinterpreted. Defaulting to 'user' would relabel a turn."""
    with pytest.raises(UnknownRoleError):
        _role("wizard")

    assert _role("assistant") == "assistant"


# --- facts: remember and recall ------------------------------------------------


def test_remember_creates_a_fact(facts: FactRepository) -> None:
    facts.remember(user_id=USER_ID, key="preferred_name", value="Ada")

    assert facts.recall_one(user_id=USER_ID, key="preferred_name") == "Ada"


def test_remember_updates_rather_than_appending(facts: FactRepository, session: Session) -> None:
    """One key, one current answer. Two rows here would be two contradictory answers."""
    facts.remember(user_id=USER_ID, key="department", value="Support")
    facts.remember(user_id=USER_ID, key="department", value="Platform")

    assert facts.recall_one(user_id=USER_ID, key="department") == "Platform"
    assert len(facts.all_for_user(user_id=USER_ID)) == 1


def test_an_update_keeps_the_original_provenance(facts: FactRepository) -> None:
    """``source_session_id`` records where the fact came from; rewriting it loses that."""
    first = facts.remember(
        user_id=USER_ID, key="role", value="engineer", source_session_id="session-a"
    )
    facts.remember(user_id=USER_ID, key="role", value="lead")

    assert first.source_session_id == "session-a"


def test_recall_returns_every_current_fact(facts: FactRepository) -> None:
    facts.remember(user_id=USER_ID, key="a", value="1")
    facts.remember(user_id=USER_ID, key="b", value="2")

    assert dict(facts.recall(user_id=USER_ID)) == {"a": "1", "b": "2"}


def test_an_expired_fact_is_not_recalled(facts: FactRepository) -> None:
    """Expiry is a read-time filter, so the value stops applying without being destroyed."""
    facts.remember(user_id=USER_ID, key="temporary", value="stale", ttl_days=0)

    assert facts.recall_one(user_id=USER_ID, key="temporary") is None
    assert dict(facts.recall(user_id=USER_ID)) == {}


def test_an_expired_fact_is_still_visible_to_a_reviewer(facts: FactRepository) -> None:
    """The row survives, so "what was once believed" is answerable."""
    facts.remember(user_id=USER_ID, key="temporary", value="stale", ttl_days=0)

    rows = facts.all_for_user(user_id=USER_ID)

    assert [r.key for r in rows] == ["temporary"]
    # Compared against the row's own created_at, not against utc_now(). SQLite does not preserve
    # tzinfo, so a value read back is naive while utc_now() is aware, and comparing the two raises
    # TypeError rather than failing an assertion. Both fields come from the same row, so both are
    # naive and the comparison is meaningful.
    assert rows[0].expires_at <= rows[0].created_at


def test_remembering_again_revives_an_expired_key(facts: FactRepository) -> None:
    """An operator correcting a stale fact must not hit a unique-constraint error."""
    facts.remember(user_id=USER_ID, key="team", value="old", ttl_days=0)

    facts.remember(user_id=USER_ID, key="team", value="new")

    assert facts.recall_one(user_id=USER_ID, key="team") == "new"
    assert len(facts.all_for_user(user_id=USER_ID)) == 1


def test_facts_are_scoped_by_user(facts: FactRepository) -> None:
    facts.remember(user_id=USER_ID, key="secret", value="mine")

    assert facts.recall_one(user_id=OTHER_USER, key="secret") is None
    assert dict(facts.recall(user_id=OTHER_USER)) == {}


def test_forget_removes_the_row(facts: FactRepository) -> None:
    """A hard delete, unlike expiry: this is a person asking to be forgotten."""
    facts.remember(user_id=USER_ID, key="gone", value="x")

    assert facts.forget(user_id=USER_ID, key="gone") is True
    assert facts.recall_one(user_id=USER_ID, key="gone") is None
    assert facts.all_for_user(user_id=USER_ID) == ()


def test_forget_reports_whether_anything_was_removed(facts: FactRepository) -> None:
    assert facts.forget(user_id=USER_ID, key="never-existed") is False


def test_purge_removes_only_expired_rows(facts: FactRepository) -> None:
    facts.remember(user_id=USER_ID, key="keep", value="current")
    facts.remember(user_id=USER_ID, key="drop", value="stale", ttl_days=0)

    removed = facts.purge_expired()

    assert removed == 1
    assert [r.key for r in facts.all_for_user(user_id=USER_ID)] == ["keep"]


def test_purge_on_a_clean_table_reports_nothing_removed(facts: FactRepository) -> None:
    facts.remember(user_id=USER_ID, key="keep", value="current")

    assert facts.purge_expired() == 0


def test_purge_spans_users(facts: FactRepository) -> None:
    """The sweep is an operator action over the whole table, not one person's data."""
    facts.remember(user_id=USER_ID, key="a", value="x", ttl_days=0)
    facts.remember(user_id=OTHER_USER, key="b", value="y", ttl_days=0)

    assert facts.purge_expired() == 2


# --- the integration proof ----------------------------------------------------


async def test_a_write_proposed_in_a_fresh_session_does_not_hit_the_foreign_key(
    session: Session, engine: Engine
) -> None:
    """The specific bug Stage 8 recorded, proven fixed through the real propose path.

    Nothing here is a mock: a session is ensured exactly as the handler does it, and the approval
    row is written against it. Before this, the insert would have raised a foreign-key violation —
    which is why the test asserts on the state rather than on the absence of an exception. An
    exception would fail the test either way; asserting the state proves the row is genuinely
    there and pending.
    """
    from agent_service.db.repositories.approvals import ApprovalRepository

    sessions = SessionRepository(session)
    sessions.ensure(SESSION_ID, user_id=USER_ID)

    approval = ApprovalRepository(session).propose(
        session_id=SESSION_ID,
        user_id=USER_ID,
        action="create_ticket",
        payload={"title": "about the printer"},
    )

    assert approval.state == ApprovalState.PENDING.value
    assert approval.session_id == SESSION_ID
