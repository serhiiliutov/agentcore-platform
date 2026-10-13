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
        moment = now or utc_now()
        stored = Message(
            session_id=session_id,
            user_id=user_id,
            sequence=self._next_sequence(session_id),
            role=message.role,
            content=message.content,
            tool_name=message.name,
            tool_call_id=message.tool_call_id,
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
            row = Message(
                session_id=session_id,
                user_id=user_id,
                sequence=start + offset,
                role=message.role,
                content=message.content,
                tool_name=message.name,
                tool_call_id=message.tool_call_id,
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

    def _next_sequence(self, session_id: str) -> int:
        """The next position in this session, starting at 1."""
        highest = self._session.execute(
            select(func.max(Message.sequence)).where(Message.session_id == session_id)
        ).scalar_one_or_none()
        return (highest or 0) + 1


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
