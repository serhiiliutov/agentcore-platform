"""Listing a user's conversations — the read a chat front end resumes from.

One feature spanning three layers, so they are tested together: the repository query that finds a
user's sessions, the route that serves them, and the factory binding that converts between the two.
Tested apart, the property that actually matters would be asserted nowhere — that what a person sees
is *their* conversations, newest first, each recognisable by its opening line.

Why the capability exists: `CONTRACT.md` is one request in and one reply out, so the contract has
nowhere to ask "which conversations do I have?". A front end without this can only continue the
conversation it is already in, and every reload strands the previous one — which is what the Chainlit
chat did until this was added.

Hermetic: in-memory SQLite and injected handlers. No server, no model, no network.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import agent_service.db.models  # noqa: F401 - registers every table on Base.metadata
from agent_service.api.app import create_app
from agent_service.api.ui_routes import SessionSummary, TranscriptMessage
from agent_service.config import Settings
from agent_service.db.base import Base, create_session_factory, register_sqlite_pragmas, utc_now
from agent_service.db.repositories.sessions import (
    PREVIEW_CHARS,
    SessionRepository,
)
from agent_service.factory import build_session_listing, build_transcript_reader
from agent_service.providers.base import Message

#: Two users, so every scoping assertion has something to leak to.
ALICE = "alice"
BOB = "bob"


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite with the pragmas production uses, so the schema behaves the same."""
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
def sessions(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"database_url": "sqlite+pysqlite:///:memory:"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _say(
    sessions: sessionmaker[Session],
    *,
    session_id: str,
    user_id: str,
    prompted: str,
    answered: str = "Done.",
    minutes_ago: int = 0,
) -> None:
    """Store one whole turn: what the user asked and what the assistant replied.

    Both sides, because a listing that counted only one of them would still look right — and both
    sides is what the agent's own loop writes, so this is the shape the read has to handle.
    """
    with sessions() as session:
        repo = SessionRepository(session)
        repo.ensure(session_id, user_id=user_id)
        moment = utc_now() - timedelta(minutes=minutes_ago)
        repo.append(
            session_id,
            user_id=user_id,
            message=Message(role="user", content=prompted),
            now=moment,
        )
        repo.append(
            session_id,
            user_id=user_id,
            message=Message(role="assistant", content=answered),
            now=moment + timedelta(seconds=1),
        )
        session.commit()


# --- the repository ----------------------------------------------------------


def test_a_users_conversations_are_listed_most_recently_active_first(
    sessions: sessionmaker[Session],
) -> None:
    """Ordered by the newest message, not by which conversation started first.

    The two differ in the case that matters: somebody looking for the conversation they were *last*
    in is not looking for the one they opened first, and ordering by the session row's age would
    put a long-running conversation they have returned to at the bottom.
    """
    _say(sessions, session_id="older", user_id=ALICE, prompted="the printer one", minutes_ago=30)
    _say(sessions, session_id="newer", user_id=ALICE, prompted="the vpn one", minutes_ago=1)

    with sessions() as session:
        found = SessionRepository(session).for_user(user_id=ALICE)

    assert [row.session_id for row in found] == ["newer", "older"]


def test_the_preview_is_what_the_user_said_not_what_they_were_told(
    sessions: sessionmaker[Session],
) -> None:
    """The opening prompt is what a person recognises a conversation by.

    The assistant's reply would be a plausible-looking preview and the wrong one: "Done. I used 1
    tool call(s) to answer." is identical across every conversation this agent has.
    """
    _say(
        sessions,
        session_id="s1",
        user_id=ALICE,
        prompted="find tickets about the printer",
        answered="Done. I used 1 tool call(s) to answer.",
    )

    with sessions() as session:
        found = SessionRepository(session).for_user(user_id=ALICE)

    assert found[0].preview == "find tickets about the printer"


def test_one_users_conversations_are_invisible_to_another(
    sessions: sessionmaker[Session],
) -> None:
    """The assertion the whole endpoint rests on.

    A listing is a transcript leak with a friendly summary on top: the preview is the user's own
    opening words. Scoping is therefore the feature, not a filter applied to it.
    """
    _say(sessions, session_id="alices", user_id=ALICE, prompted="my laptop is broken")
    _say(sessions, session_id="bobs", user_id=BOB, prompted="my phone is broken")

    with sessions() as session:
        seen_by_bob = SessionRepository(session).for_user(user_id=BOB)

    assert [row.session_id for row in seen_by_bob] == ["bobs"]
    assert all("laptop" not in row.preview for row in seen_by_bob)


def test_a_long_opening_is_truncated_to_one_bounded_line(
    sessions: sessionmaker[Session],
) -> None:
    """Twenty previews have to fit somewhere, and a prompt can contain a newline.

    Whitespace is collapsed and the length bounded, so a row in a list stays a row. The ellipsis is
    the visible sign that something was cut rather than the prompt simply being short.
    """
    _say(
        sessions,
        session_id="long",
        user_id=ALICE,
        prompted="first line\nsecond line " + "and then a great deal more text " * 6,
    )

    with sessions() as session:
        found = SessionRepository(session).for_user(user_id=ALICE)

    preview = found[0].preview
    assert "\n" not in preview
    assert len(preview) <= PREVIEW_CHARS
    assert preview.endswith("…")


def test_a_conversation_with_no_messages_is_not_offered(
    sessions: sessionmaker[Session],
) -> None:
    """A session row nobody has written to is not a conversation.

    Reachable through the agent's own path — `ensure` runs before the loop, so a turn that failed
    before storing anything leaves exactly this row behind — and offering it would put an empty
    thing in the list with no preview to recognise it by.
    """
    with sessions() as session:
        SessionRepository(session).ensure("empty", user_id=ALICE)
        session.commit()

    with sessions() as session:
        found = SessionRepository(session).for_user(user_id=ALICE)

    assert found == ()


def test_the_listing_counts_both_sides_of_each_turn(
    sessions: sessionmaker[Session],
) -> None:
    """Two rows per turn, because that is what the loop stores: a question and an answer."""
    _say(sessions, session_id="s1", user_id=ALICE, prompted="one")
    _say(sessions, session_id="s1", user_id=ALICE, prompted="two")

    with sessions() as session:
        found = SessionRepository(session).for_user(user_id=ALICE)

    assert found[0].messages == 4


def test_the_limit_bounds_how_many_conversations_come_back(
    sessions: sessionmaker[Session],
) -> None:
    """A picker is a menu, not an archive — and the bound is applied by the query."""
    for index in range(5):
        _say(
            sessions,
            session_id=f"s{index}",
            user_id=ALICE,
            prompted=f"question {index}",
            minutes_ago=index,
        )

    with sessions() as session:
        found = SessionRepository(session).for_user(user_id=ALICE, limit=3)

    assert [row.session_id for row in found] == ["s0", "s1", "s2"]


# --- the route ---------------------------------------------------------------


def _client(
    *,
    listing: Callable[[str], Awaitable[Sequence[SessionSummary]]] | None = None,
) -> TestClient:
    return TestClient(create_app(_settings(), session_listing=listing))


async def _one_session(_user_id: str) -> Sequence[SessionSummary]:
    return [SessionSummary(session_id="s1", messages=2, preview="the printer one")]


def test_the_listing_is_served_with_the_fields_a_picker_draws_from() -> None:
    client = _client(listing=_one_session)

    body = client.get("/api/sessions", params={"user_id": ALICE}).json()

    assert body["sessions"][0]["session_id"] == "s1"
    assert body["sessions"][0]["messages"] == 2
    assert body["sessions"][0]["preview"] == "the printer one"
    assert body["sessions"][0]["last_at"] is None


def test_the_user_id_is_required() -> None:
    """Without one there is no safe answer, so FastAPI rejects the request before the handler runs.

    Not a nicety: an unscoped listing would return the opening line of every conversation in the
    system to whoever asked, which is the disclosure every user-scoped read in this service exists
    to prevent.
    """
    response = _client(listing=_one_session).get("/api/sessions")

    assert response.status_code == 422


def test_an_unwired_listing_answers_501_rather_than_500() -> None:
    """"Not configured" stays distinguishable from "broken", as on the other endpoints."""
    response = _client().get("/api/sessions", params={"user_id": ALICE})

    assert response.status_code == 501
    assert response.json()["error"]["kind"] == "NotImplemented"
    assert "session listing" in response.json()["error"]["detail"]


def test_an_empty_listing_is_an_empty_list_not_an_error() -> None:
    """A user with no conversations is the normal first-run state, not a failure."""

    async def nothing(_user_id: str) -> Sequence[SessionSummary]:
        return []

    body = _client(listing=nothing).get("/api/sessions", params={"user_id": ALICE}).json()

    assert body == {"sessions": []}


# --- the binding, end to end through the factory -----------------------------


async def test_the_wired_listing_reads_the_database_and_maps_to_the_wire_shape(
    sessions: sessionmaker[Session],
) -> None:
    """The one conversion between the repository's dataclass and the API's model.

    Asserted through the real binding rather than by comparing the two shapes by hand, because the
    failure this guards against is exactly a field that exists on one and not the other — a preview
    dropped in translation would leave the picker with a list of ids.
    """
    _say(sessions, session_id="s1", user_id=ALICE, prompted="find the printer ticket")

    listing = build_session_listing(sessions=sessions)
    found = await listing(ALICE)

    assert isinstance(found[0], SessionSummary)
    assert found[0].session_id == "s1"
    assert found[0].preview == "find the printer ticket"
    assert found[0].messages == 2
    assert found[0].last_at is not None


# --- reading one conversation -------------------------------------------------
#
# The second half of "select a conversation". A listing that offered a conversation the front end
# could not display would leave an empty screen above full history, which reads as loss — so the
# read that redraws one is part of the same feature and is tested here beside it.


def _store_tool_row(
    sessions: sessionmaker[Session],
    *,
    session_id: str,
    user_id: str,
    content: str = '{"tickets": []}',
    tool_name: str = "search_tickets",
) -> None:
    """Store a tool result, so a transcript has a row that is neither a question nor an answer."""
    with sessions() as session:
        repo = SessionRepository(session)
        repo.ensure(session_id, user_id=user_id)
        repo.append(
            session_id,
            user_id=user_id,
            message=Message(
                role="tool",
                content=content,
                name=tool_name,
                tool_call_id="call-1",
            ),
        )
        session.commit()


def test_a_transcript_reads_the_conversation_oldest_first(
    sessions: sessionmaker[Session],
) -> None:
    """Ordered by ``sequence``, which is the only thing that orders a conversation correctly.

    Timestamps cannot do it — two turns can share one — and the ordering is the whole point: a
    replay that shows the answer before the question is worse than no replay.
    """
    _say(sessions, session_id="s1", user_id=ALICE, prompted="first question", answered="first answer")
    _say(
        sessions,
        session_id="s1",
        user_id=ALICE,
        prompted="second question",
        answered="second answer",
    )

    with sessions() as session:
        entries = SessionRepository(session).transcript("s1", user_id=ALICE)

    assert [entry.role for entry in entries] == ["user", "assistant", "user", "assistant"]
    assert [entry.content for entry in entries] == [
        "first question",
        "first answer",
        "second question",
        "second answer",
    ]
    assert [entry.sequence for entry in entries] == [1, 2, 3, 4]


def test_a_transcript_keeps_the_tool_name_a_replay_needs(
    sessions: sessionmaker[Session],
) -> None:
    """A tool row is stored as one, so it is read as one.

    Flattening every row to plain text would render a tool result as though the assistant had said
    it — which is exactly the distinction the stored ``tool_name`` exists to preserve.
    """
    _say(sessions, session_id="s1", user_id=ALICE, prompted="find the printer")
    _store_tool_row(sessions, session_id="s1", user_id=ALICE)

    with sessions() as session:
        entries = SessionRepository(session).transcript("s1", user_id=ALICE)

    tool_rows = [entry for entry in entries if entry.role == "tool"]
    assert len(tool_rows) == 1
    assert tool_rows[0].tool_name == "search_tickets"
    assert tool_rows[0].created_at is not None


def test_another_users_transcript_is_empty_rather_than_refused(
    sessions: sessionmaker[Session],
) -> None:
    """The non-oracle rule, and here it protects the conversation itself.

    A listing leaks opening lines; a transcript leaks everything. Returning an empty conversation
    for somebody else's id means a guessed id cannot even be confirmed to exist — a "forbidden"
    would confirm it. Same rule `SessionRepository.ensure` documents, applied to the read where it
    matters most.
    """
    _say(sessions, session_id="alices", user_id=ALICE, prompted="my laptop is broken")

    with sessions() as session:
        seen_by_bob = SessionRepository(session).transcript("alices", user_id=BOB)

    assert seen_by_bob == ()


def test_a_transcript_limit_keeps_the_end_of_the_conversation(
    sessions: sessionmaker[Session],
) -> None:
    """Bounded at the newest end, because the end is what somebody came back for."""
    for index in range(4):
        _say(sessions, session_id="s1", user_id=ALICE, prompted=f"question {index}")

    with sessions() as session:
        entries = SessionRepository(session).transcript("s1", user_id=ALICE, limit=2)

    assert [entry.content for entry in entries] == ["question 3", "Done."]


def test_an_unknown_session_reads_as_an_empty_transcript(
    sessions: sessionmaker[Session],
) -> None:
    """A session that never existed and one that is not yours are the same answer, deliberately."""
    with sessions() as session:
        assert SessionRepository(session).transcript("never-existed", user_id=ALICE) == ()


async def _two_messages(_session_id: str, _user_id: str) -> Sequence[TranscriptMessage]:
    return [
        TranscriptMessage(sequence=1, role="user", content="the printer one"),
        TranscriptMessage(sequence=2, role="assistant", content="Done."),
    ]


def _transcript_client(
    *,
    reader: Callable[[str, str], Awaitable[Sequence[TranscriptMessage]]] | None = None,
) -> TestClient:
    return TestClient(create_app(_settings(), transcript_reader=reader))


def test_the_transcript_route_serves_the_messages_and_names_the_conversation() -> None:
    client = _transcript_client(reader=_two_messages)

    body = client.get("/api/sessions/s1/messages", params={"user_id": ALICE}).json()

    assert body["session_id"] == "s1"
    assert [message["role"] for message in body["messages"]] == ["user", "assistant"]
    assert body["messages"][0]["content"] == "the printer one"


def test_the_transcript_route_requires_a_user_id() -> None:
    """The owner check cannot be skipped by omitting the owner, so it is required and validated."""
    response = _transcript_client(reader=_two_messages).get("/api/sessions/s1/messages")

    assert response.status_code == 422


def test_an_unwired_transcript_reader_answers_501() -> None:
    response = _transcript_client().get("/api/sessions/s1/messages", params={"user_id": ALICE})

    assert response.status_code == 501
    assert response.json()["error"]["kind"] == "NotImplemented"


async def test_the_wired_transcript_reader_returns_rows_in_the_wire_shape(
    sessions: sessionmaker[Session],
) -> None:
    """The binding, end to end: a stored conversation becomes the model the route serialises."""
    _say(sessions, session_id="s1", user_id=ALICE, prompted="find the printer ticket")

    reader = build_transcript_reader(sessions=sessions)
    found = await reader("s1", ALICE)

    assert [message.role for message in found] == ["user", "assistant"]
    assert found[0].content == "find the printer ticket"
    assert isinstance(found[0], TranscriptMessage)
    # And it is still scoped: the same conversation read by somebody else is nothing.
    assert await reader("s1", BOB) == []
