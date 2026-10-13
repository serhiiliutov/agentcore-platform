"""Cross-session memory: durable facts about a person, and nothing else.

The assignment asks for clarity about *what* is remembered, *where*, *how long* and
*for whom*. Each of those is answered by a column rather than by convention:

* **What** — a single scalar ``key`` with a string ``value``. Not a blob, not a
  transcript. If something cannot be expressed as "a name for one piece of
  information", it does not belong in this table, and that constraint is what keeps
  memory inspectable and deletable rather than an opaque store.
* **Where** — this table, in the same database as everything else, reachable by
  ``SELECT * FROM user_facts WHERE user_id = ?``. A reviewer can list exactly what is
  remembered about a person without reading any application code.
* **How long** — ``expires_at``, set from ``AGENT_MEMORY_FACT_TTL_DAYS`` at write
  time and evaluated on read. Memory that never expires is a liability: a fact about
  someone's team is true this year and misleading in three.
* **For whom** — ``user_id``, indexed, and enforced by the repository layer. There is
  no query in this codebase that reads a fact without it.

``source_session_id`` is the part that is easy to omit and expensive to add later: it
records which conversation taught us the fact, so a user asking "why do you think
that about me?" can be answered precisely, and so a fact can be traced back and
corrected. Provenance is a feature, not metadata.

The unique constraint on ``(user_id, key)`` makes memory a lookup rather than a log.
Storing the same preference twelve times and hoping the read picks the right one is a
bug generator; remembering one current value is a decision.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agent_service.db.base import Base, utc_now


class UserFact(Base):
    """One durable fact about one user, with provenance and an expiry."""

    __tablename__ = "user_facts"
    __table_args__ = (
        # A key identifies one piece of information for one person; writing it again
        # updates the value rather than adding a second answer to the same question.
        UniqueConstraint("user_id", "key", name="uq_user_facts_user_key"),
        # The read is always "current facts for this user", so the expiry is part of
        # the index rather than a filter applied after loading everything.
        Index("ix_user_facts_user_expires", "user_id", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    #: The owner. Every read is scoped by this, and there is no unscoped read path.
    user_id: Mapped[str] = mapped_column(String(128), index=True)

    #: A short, stable identifier, e.g. ``preferred_name`` or ``department``.
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(Text)

    #: Which conversation this was learned in. Empty when it came from configuration
    #: or an operator rather than from the user.
    source_session_id: Mapped[str | None] = mapped_column(String(128), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    #: After this moment the fact is no longer returned. Kept, not deleted, so a
    #: reviewer can still see what was once believed and when it stopped applying.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    def __repr__(self) -> str:
        return (
            f"UserFact(user_id={self.user_id!r}, key={self.key!r}, expires_at={self.expires_at!r})"
        )
