"""Conversation state: sessions and their messages.

The model is named ``ChatSession`` rather than ``Session`` only to avoid colliding
with SQLAlchemy's own ``Session`` in every file that imports both. The table is
``sessions``.

Two decisions worth stating:

* **The primary key is the caller's session id.** AgentCore sends
  ``X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`` and expects short-term memory to be
  keyed by it. Generating our own id would mean maintaining a mapping between the
  two for no benefit, and would break the contract's promise that the same header
  value resumes the same conversation.
* **``user_id`` is denormalised onto messages as well as sessions.** Strictly it is
  reachable through the parent, but the invariant this repository has to guarantee is
  "no query returns another user's data". Carrying the owner on the row makes that
  checkable without a join, and makes it impossible to write a message query that is
  accidentally unscoped.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agent_service.db.base import Base, utc_now

if TYPE_CHECKING:
    from agent_service.db.models.approval import Approval

#: Roles a stored message can have. Matches the provider-neutral message model.
MESSAGE_ROLES = ("system", "user", "assistant", "tool")


class ChatSession(Base):
    """One conversation, keyed by the runtime's session id."""

    __tablename__ = "sessions"

    #: The AgentCore session id, taken verbatim from the request header.
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    #: Who the conversation belongs to. Every read is scoped by this.
    user_id: Mapped[str] = mapped_column(String(128), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, index=True
    )

    messages: Mapped[list[Message]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Message.sequence",
    )
    approvals: Mapped[list[Approval]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"ChatSession(id={self.id!r}, user_id={self.user_id!r})"


class Message(Base):
    """A single turn in a conversation, in order."""

    __tablename__ = "messages"
    __table_args__ = (
        # The common read is "the last N turns of one session, oldest first".
        Index("ix_messages_session_sequence", "session_id", "sequence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
    )
    #: Denormalised owner, so a message query can be scoped without a join.
    user_id: Mapped[str] = mapped_column(String(128), index=True)

    #: Monotonic position within the session. Ordering by ``created_at`` alone is a
    #: bug waiting to happen: two turns can share a timestamp.
    sequence: Mapped[int] = mapped_column()

    #: One of ``MESSAGE_ROLES``. Stored as text so the DB stays readable.
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")

    #: Set on tool results and assistant turns that request a tool.
    tool_name: Mapped[str | None] = mapped_column(String(64), default=None)
    tool_call_id: Mapped[str | None] = mapped_column(String(128), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    session: Mapped[ChatSession] = relationship(back_populates="messages")

    def __repr__(self) -> str:
        preview = self.content[:40].replace("\n", " ")
        return f"Message(session={self.session_id!r}, seq={self.sequence}, role={self.role!r}, {preview!r})"
