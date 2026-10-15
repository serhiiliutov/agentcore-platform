"""Sessions and their messages: the short-term memory the loop reads.

Two jobs, and the first is a fix rather than a feature.

**``ensure`` closes a real gap.** Stage 8's `InvocationHandlerFactory` mints a session id
without inserting a row, while ``approvals.session_id`` is a foreign key onto ``sessions.id``
with ``PRAGMA foreign_keys=ON``. So the first write proposed in a fresh conversation would fail
with a foreign key violation — not a hypothetical, a certain one. ``ensure`` is get-or-create,
and every caller that is about to do anything session-scoped calls it first.

**``history`` is what makes a conversation a conversation.** The loop already accepts a
``history`` sequence and Stage 8 passed nothing. This produces it from stored rows, ordered by
``sequence`` rather than by ``created_at``: two turns can share a timestamp, and ordering by one
would silently reorder a conversation about half the time.

The mapping to `Message` is deliberately lossy in one direction and not the other. A stored
message keeps its tool name and call id, because a provider needs those to match a call to its
result; a `Message` carries only what a provider needs to see. Storing more than we can send
would mean the transcript and the payload could disagree, and only one of them is the truth.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent_service.db.base import utc_now
from agent_service.db.models.conversation import ChatSession, Message
from agent_service.providers.base import Message as ProviderMessage, ToolCallRequest

#: The default number of stored turns a provider is shown. Bounded because a long
#: conversation would otherwise grow the prompt without limit, and because the interesting
#: context is nearly always the recent part.
DEFAULT_HISTORY_LIMIT = 40

#: How many of a user's conversations a listing returns. Bounded because a picker is a menu, not an
#: archive: somebody resuming a conversation wants the last handful, and a thousand-row dropdown is
#: unusable in a way that "load more" is not.
DEFAULT_SESSION_LIMIT = 20

#: How much of a conversation's opening line a preview carries. Long enough to recognise the
#: conversation by, short enough that twenty of them fit in one message.
PREVIEW_CHARS = 80

#: How many stored messages a transcript read returns. Larger than the picker's limit because a
#: conversation is the thing somebody asked to see, not a menu of them — but still bounded, since a
#: front end rendering ten thousand messages is a worse experience than one rendering the last two
#: hundred.
DEFAULT_TRANSCRIPT_LIMIT = 200


@dataclass(frozen=True)
class SessionOverview:
    """One conversation, as a picker needs to show it.

    A dataclass rather than the API model, because this layer does not know the wire — and the
    mapping between the two belongs where the other repository-to-response mappings live, in the
    composition root. `as_pending_approval` is the same split for the same reason.
    """

    session_id: str
    #: How many stored turns it holds — the user's and the assistant's together.
    messages: int
    #: When it was last written to. A session always has at least one message to appear here, so
    #: this is never ``None`` in practice; the type keeps the aggregate honest.
    last_at: datetime | None
    #: The opening of the first thing the *user* said, which is what a person recognises.
    preview: str


@dataclass(frozen=True)
class TranscriptEntry:
    """One stored message, as a front end replaying a conversation needs it.

    Deliberately closer to the row than to the provider's `Message`. A `Message` is lossy on
    purpose — it carries only what a provider needs to see — while a replay has to show a person
    what actually happened, which means keeping the sequence, the timestamp and the tool's name.
    The two reads share a table and nothing else.

    **What a conversation contains is decided elsewhere, and it is worth knowing here.**
    ``agent/handler.py::_turn_messages`` stores the prompt and the final answer and nothing in
    between — deliberately, so that memory stays a conversation rather than a transcript — so in
    practice every row reaching this read is a user or an assistant turn and ``tool_name`` is
    ``None`` throughout. The field is kept because the column exists and a row carrying one would
    otherwise be silently dropped, not because tool calls are expected here.
    """

    sequence: int
    #: ``system``, ``user``, ``assistant`` or ``tool``. Passed through as a string rather than
    #: narrowed here, because the only consumer is a front end deciding how to render it.
    role: str
    content: str
    #: Set on a tool call or a tool result, so a replay can label the row instead of showing JSON.
    tool_name: str | None
    created_at: datetime | None


class SessionRepository:
    """Reads and writes over ``sessions`` and ``messages`` for one user."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def ensure(self, session_id: str, *, user_id: str) -> ChatSession:
        """Return the session, creating it if this is the first turn.

        Get-or-create rather than create, because the second turn of a conversation must not
        fail on a duplicate primary key — and rather than get, because the first turn must not
        leave the caller with a session id that has no row.

        A session that exists but belongs to a *different* user is returned as it is, and the
        caller will find no history and no approvals for it. That is deliberate: rejecting it
        here would turn a guessed session id into a distinguishable "this exists but is not
        yours", which is exactly the oracle Stage 2 and Stage 8 both avoid.
        """
        existing = self._session.get(ChatSession, session_id)
        if existing is not None:
            return existing

        created = ChatSession(id=session_id, user_id=user_id)
        self._session.add(created)
        self._session.flush()
        return created

    def history(
        self,
        session_id: str,
        *,
        user_id: str,
        limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> tuple[ProviderMessage, ...]:
        """The most recent stored turns, oldest first, ready for a provider.

        Scoped by ``user_id`` as well as by session, because the denormalised owner on each
        message is what makes an unscoped read impossible rather than merely unlikely.

        The last ``limit`` rows are selected in descending order and then reversed, so the
        bound applies to the *newest* turns. Selecting ascending and slicing would return the
        beginning of a long conversation and drop the part that matters.
        """
        rows = (
            self._session.execute(
                select(Message)
                .where(Message.session_id == session_id, Message.user_id == user_id)
                .order_by(Message.sequence.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )

        return tuple(_to_provider(row) for row in reversed(rows))

    def append(
        self,
        session_id: str,
        *,
        user_id: str,
        message: ProviderMessage,
        now: datetime | None = None,
    ) -> Message:
        """Store one turn, assigning the next sequence number.

        ``sequence`` is computed as ``max + 1`` rather than taken from a counter, because the
        session row is the only shared state and a counter on it would need a lock to stay
        correct — while ``max`` is a single indexed read that cannot drift.
        """
        call_name, call_id = _stored_call(message)
        moment = now or utc_now()
        stored = Message(
            session_id=session_id,
            user_id=user_id,
            sequence=self._next_sequence(session_id),
            role=message.role,
            content=message.content,
            tool_name=call_name,
            tool_call_id=call_id,
            created_at=moment,
        )
        self._session.add(stored)
        self._session.flush()
        return stored

    def append_all(
        self, session_id: str, *, user_id: str, messages: Sequence[ProviderMessage]
    ) -> tuple[Message, ...]:
        """Store several turns in order, numbering them consecutively."""
        start = self._next_sequence(session_id)
        stored: list[Message] = []
        for offset, message in enumerate(messages):
            call_name, call_id = _stored_call(message)
            row = Message(
                session_id=session_id,
                user_id=user_id,
                sequence=start + offset,
                role=message.role,
                content=message.content,
                tool_name=call_name,
                tool_call_id=call_id,
            )
            self._session.add(row)
            stored.append(row)
        self._session.flush()
        return tuple(stored)

    def count(self, session_id: str, *, user_id: str) -> int:
        """How many turns this session holds, for a test or an operator."""
        return self._session.execute(
            select(func.count())
            .select_from(Message)
            .where(Message.session_id == session_id, Message.user_id == user_id)
        ).scalar_one()

    def for_user(
        self, *, user_id: str, limit: int = DEFAULT_SESSION_LIMIT
    ) -> tuple[SessionOverview, ...]:
        """This user's conversations, most recently active first.

        A read the Runtime contract has no room for: `CONTRACT.md` is one request in and one reply
        out, so nothing in it ever asks "which conversations do I have?". Without this a front end
        can only continue the conversation it happens to be in, and every reload strands the
        previous one — which is exactly what the Chainlit front end did until this existed.

        Ordered by the newest message in each session rather than by the session row's own age, and
        the two differ in the case that matters: what a person recognises is the conversation they
        were *last* in, not the one they started first.

        Scoped by ``user_id`` like every other read here, and that is the point of the endpoint
        rather than a detail — a listing that leaked would show one person another's conversations a
        line at a time, opening lines included.

        Two queries rather than one, and deliberately not one per row: the aggregate gives counts
        and ordering, then the opening lines are read for those sessions alone. A preview fetched
        inside a loop is the shape this would drift into, and against a single table it is easy not
        to notice.
        """
        # `Message` alone is enough. A conversation is a thing that has messages, so a session row
        # with none is not something a picker should offer to resume — which also keeps this from
        # having to know anything about `ChatSession`.
        recent = self._session.execute(
            select(
                Message.session_id,
                func.count().label("messages"),
                func.max(Message.created_at).label("last_at"),
            )
            .where(Message.user_id == user_id)
            .group_by(Message.session_id)
            .order_by(func.max(Message.created_at).desc())
            .limit(limit)
        ).all()
        if not recent:
            return ()

        openings = self._openings(user_id=user_id, session_ids=[row.session_id for row in recent])

        return tuple(
            SessionOverview(
                session_id=row.session_id,
                messages=row.messages,
                last_at=row.last_at,
                preview=openings.get(row.session_id, ""),
            )
            for row in recent
        )

    def transcript(
        self,
        session_id: str,
        *,
        user_id: str,
        limit: int = DEFAULT_TRANSCRIPT_LIMIT,
    ) -> tuple[TranscriptEntry, ...]:
        """One conversation's stored messages, oldest first, for a front end to redraw.

        Why this exists beside `history`, which reads the same table: they answer different
        questions and must not converge. `history` is what the *model* is shown — provider-shaped,
        lossy, capped tight because a long conversation costs tokens — while this is what a *person*
        is shown: every row, in order, with the tool name and the timestamp. Serving a replay from
        `history` would silently drop the middle of a long conversation and render the model's view
        as if it were the whole truth.

        Scoped by ``user_id`` for the same reason the listing is, and the consequence of getting it
        wrong is worse here: a listing leaks opening lines, a transcript leaks the conversation. A
        session belonging to somebody else therefore reads as an empty transcript rather than as a
        refusal — the same non-oracle `ensure` documents, where an id that is not yours must be
        indistinguishable from one that does not exist.

        The newest ``limit`` rows, then reversed. Bounding the newest end is what keeps a very long
        conversation useful: the beginning is what a person already knows, and the end is what they
        came back for.
        """
        rows = (
            self._session.execute(
                select(Message)
                .where(Message.session_id == session_id, Message.user_id == user_id)
                .order_by(Message.sequence.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )

        return tuple(
            TranscriptEntry(
                sequence=row.sequence,
                role=row.role,
                content=row.content,
                tool_name=row.tool_name,
                created_at=row.created_at,
            )
            for row in reversed(rows)
        )

    def _openings(self, *, user_id: str, session_ids: Sequence[str]) -> dict[str, str]:
        """The first thing the *user* said in each of these sessions, truncated.

        The user's own words rather than the assistant's or a tool result's, because the opening
        prompt is what somebody recognises a conversation by — "the printer one" is a sentence they
        typed, not one they were answered with.

        Ranked with a window function rather than read and filtered in Python: the first user
        message is one row out of however many the conversation holds, and reading them all to keep
        twenty would make this read grow with the transcript it is summarising.
        """
        ranked = (
            select(
                Message.session_id.label("session_id"),
                Message.content.label("content"),
                func.row_number()
                .over(partition_by=Message.session_id, order_by=Message.sequence)
                .label("position"),
            )
            .where(
                Message.user_id == user_id,
                Message.session_id.in_(session_ids),
                Message.role == "user",
            )
            .subquery()
        )

        rows = self._session.execute(
            select(ranked.c.session_id, ranked.c.content).where(ranked.c.position == 1)
        ).all()
        return {row.session_id: _preview(row.content) for row in rows}

    def _next_sequence(self, session_id: str) -> int:
        """The next position in this session, starting at 1."""
        highest = self._session.execute(
            select(func.max(Message.sequence)).where(Message.session_id == session_id)
        ).scalar_one_or_none()
        return (highest or 0) + 1


def _preview(content: str) -> str:
    """The opening of a conversation, on one line and bounded.

    Whitespace is collapsed first, because a prompt with a newline in it would otherwise render as
    several lines in a list where every row is meant to be one. Truncation is at a character count
    rather than a word, so twenty previews cannot vary wildly in length — and the ellipsis is
    appended only when something was actually cut, so a short prompt reads as itself.
    """
    collapsed = " ".join(content.split())
    if len(collapsed) <= PREVIEW_CHARS:
        return collapsed
    return collapsed[: PREVIEW_CHARS - 1].rstrip() + "…"


def _stored_call(message: ProviderMessage) -> tuple[str | None, str | None]:
    """The call identity to store against a message.

    Two sources, and the order matters. A tool *result* names its call directly through
    ``name``/``tool_call_id``. An assistant turn instead carries one or more `ToolCallRequest`s
    in ``tool_calls`` and nothing in those two fields — which is how the flat ``messages`` row
    ended up storing ``None`` for both and losing the link entirely.

    Only the FIRST call is stored. The row has one pair of columns, so an assistant turn that
    called three tools cannot be represented completely; keeping the first means a single-call
    turn round-trips exactly, and a multi-call turn keeps enough to show what was called. The
    alternative — one row per call — would make a turn's row count depend on the model's
    behaviour, and reconstructing a single message from several rows at read time is the
    complication the flat schema exists to avoid.
    """
    if message.name or message.tool_call_id:
        return message.name, message.tool_call_id
    if message.tool_calls:
        first = message.tool_calls[0]
        return first.name, first.id
    return None, None


def _to_provider(row: Message) -> ProviderMessage:
    """A stored row as a provider-neutral message.

    An ``assistant`` row that carries a tool call id is reconstructed as a message with a
    single `ToolCallRequest`, because that is how the provider expects to see it — the stored
    shape is flatter than the payload shape, and the difference is closed here rather than in
    every caller.
    """
    if row.role == "assistant" and row.tool_call_id and row.tool_name:
        return ProviderMessage.assistant(
            row.content,
            (
                ToolCallRequest(
                    id=row.tool_call_id,
                    name=row.tool_name,
                    # The arguments are not stored per call. A provider that needs them again
                    # gets them from the tool's own result text, which the next row carries.
                    arguments={},
                ),
            ),
        )

    if row.role == "tool" and row.tool_call_id and row.tool_name:
        call = ToolCallRequest(id=row.tool_call_id, name=row.tool_name)
        return ProviderMessage.tool_result(call, row.content)

    return ProviderMessage(role=_role(row.role), content=row.content)


#: The roles a provider may see. Kept as a local set rather than imported from the model, so the
#: runtime check does not depend on the model's typing.
_KNOWN_ROLES = frozenset({"system", "user", "assistant", "tool"})


class UnknownRoleError(RuntimeError):
    """A stored message's role is not one a provider understands."""


def _role(stored: str) -> Literal["system", "user", "assistant", "tool"]:
    """Narrow a stored role to the type a provider accepts.

    The column is text and the provider's field is a union of four literals, so something has to
    bridge them. It raises rather than defaulting, because the roles are written by this service:
    an unrecognised one means the row was edited or truncated, and guessing would change what the
    model is shown. Defaulting to ``user`` would silently relabel an assistant turn as a request,
    which is the kind of corruption that produces a plausible wrong answer rather than an error.
    """
    if stored not in _KNOWN_ROLES:
        msg = (
            f"stored message has an unknown role {stored!r}; expected one of {sorted(_KNOWN_ROLES)}"
        )
        raise UnknownRoleError(msg)
    return cast('Literal["system", "user", "assistant", "tool"]', stored)
