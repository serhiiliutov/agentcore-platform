"""The trace index: one row per agent invocation.

The assignment asks that any single run be explainable afterwards, so the run has to
be findable before it can be read. That is this table's only job: it is an *index*, not
the trace itself.

**Why the spans are not in the database.** A trace is twenty to two hundred structured
events, a few of which contain tool payloads. Storing them as rows would mean either a
table with a loose JSON column (a file with extra steps), or a span table whose shape
has to anticipate every future event type. Instead each run writes a JSONL file under
``TRACE_DIR`` (one self-contained object per line, readable with ``jq`` when something
has gone wrong at 2am) and this table holds the searchable summary.

**What is indexed and why.** Every column here answers a question someone will
actually ask:

* *What happened in this session?* — ``session_id``, ``started_at``
* *What has this user been doing?* — ``user_id``, ``started_at``
* *What has been failing?* — ``status``, ``started_at``
* *How much did this run cost, and was it slow?* — ``duration_ms``, ``total_tokens``
* *Which model produced that?* — ``provider``, ``model_id``

``trace_path`` is stored so a row is self-sufficient: given a row, the spans can be
opened without guessing a filename convention from the id.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from agent_service.db.base import Base, utc_now


class Trace(Base):
    """The searchable summary of one invocation."""

    __tablename__ = "traces"
    __table_args__ = (
        # The two common listings: by session, and most recent first.
        Index("ix_traces_session_started", "session_id", "started_at"),
        Index("ix_traces_user_started", "user_id", "started_at"),
        Index("ix_traces_status_started", "status", "started_at"),
    )

    #: Correlates with the HTTP request id, so a log line and a row find each other.
    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    session_id: Mapped[str] = mapped_column(String(128), index=True)
    #: The owner. Listing a user's activity is scoped by this, like every other read.
    user_id: Mapped[str] = mapped_column(String(128), index=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    #: Stored rather than derived, because ``ended_at - started_at`` has to be computed
    #: in Python anyway and the unit is then unambiguous in the database.
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)

    #: ``ok`` | ``error``. Kept short and closed so a filter is meaningful.
    status: Mapped[str] = mapped_column(String(16), default="ok")

    provider: Mapped[str] = mapped_column(String(32), default="")
    model_id: Mapped[str] = mapped_column(String(128), default="")

    #: Counters, so "how much did this cost" is answerable without parsing spans.
    model_turns: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)

    #: The failure, in one line, when ``status`` is ``error``.
    error: Mapped[str | None] = mapped_column(Text, default=None)

    #: Where the spans live. Stored so the row alone is enough to open them.
    trace_path: Mapped[str] = mapped_column(String(512), default="")

    def __repr__(self) -> str:
        return (
            f"Trace(id={self.id!r}, session_id={self.session_id!r}, "
            f"status={self.status!r}, duration_ms={self.duration_ms!r})"
        )
