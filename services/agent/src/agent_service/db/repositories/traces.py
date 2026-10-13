"""Traces: storing a run's summary, and finding it again.

The other half of the observability split. `Tracer` describes a run; this stores the description
and answers the questions someone actually asks about it.

**Every read is scoped by ``user_id``.** Same rule as every other repository here, and for the
same reason: a trace contains a user's prompts and their tool arguments, so an unscoped read
would be the single widest disclosure in the service. There is no unscoped variant and no
default.

**A trace row is written once, at the end of a run.** Not updated as the run proceeds: a
partially-written row would claim a duration and a status the run had not reached, and the
reader would have no way to tell an in-progress run from a finished one that crashed the writer.
The spans file has the fine detail; the row is the summary, and a summary of an unfinished thing
is not a summary.

**``status`` is a closed pair.** ``ok`` or ``error``, matching the column's comment. Anything
else is rejected rather than stored, because a filter on a column with five values where someone
expected two is a filter that silently returns nothing.

``list_for_user`` and the rest return rows newest first, because the question is almost always
"what just happened" rather than "where did this begin".
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent_service.db.models.observability import Trace

#: The statuses the column may hold. A tuple rather than a Literal because the value crosses a
#: database boundary, where a Literal would only be a comment.
STATUSES = ("ok", "error")

#: How many traces a listing returns by default. Bounded so a caller cannot ask for a user's whole
#: history by accident; a caller that wants more passes a larger limit explicitly.
DEFAULT_LIST_LIMIT = 50


class TraceRepository:
    """Reads and writes over ``traces`` for one user."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(self, summary: dict[str, Any], *, user_id: str) -> Trace:
        """Store one run's summary.

        Takes the dictionary `Tracer.finish` returns and passes it into the model, which is why the
        tracer returns a dict rather than a row: the tracer should not have to import the schema to
        describe a run, and this should not have to re-derive what the tracer already knows.

        ``user_id`` is a parameter rather than read from the summary, so a caller cannot store a row
        owned by someone else by passing a summary that says so. The parameter states ownership; the
        summary is data.

        An unrecognised status is refused. Storing it would put a value in a column whose readers
        were told it holds two, and the row would then be invisible to every filter written against
        those two — a trace that exists and cannot be found is worse than one that was never
        written, because the missing one would at least have been reported.
        """
        status = summary.get("status", "ok")
        if status not in STATUSES:
            msg = f"trace status must be one of {STATUSES}, got {status!r}"
            raise ValueError(msg)

        row = Trace(
            id=summary["id"],
            session_id=summary["session_id"],
            user_id=user_id,
            started_at=summary["started_at"],
            ended_at=summary.get("ended_at"),
            duration_ms=summary.get("duration_ms"),
            status=status,
            provider=summary.get("provider", ""),
            model_id=summary.get("model_id", ""),
            model_turns=summary.get("model_turns", 0),
            tool_calls=summary.get("tool_calls", 0),
            total_tokens=summary.get("total_tokens", 0),
            error=summary.get("error"),
            trace_path=summary.get("trace_path", ""),
        )
        self._session.add(row)
        self._session.flush()
        return row

    def get(self, trace_id: str, *, user_id: str) -> Trace | None:
        """One trace, or ``None`` if it does not exist or is not this user's.

        ``None`` for both cases deliberately. Distinguishing them would let a caller probe for
        trace ids and learn which exist — the same oracle the approval and session repositories
        both refuse to provide.
        """
        return self._session.execute(
            select(Trace).where(Trace.id == trace_id, Trace.user_id == user_id)
        ).scalar_one_or_none()

    def list_for_user(self, *, user_id: str, limit: int = DEFAULT_LIST_LIMIT) -> Sequence[Trace]:
        """This user's runs, newest first."""
        return tuple(
            self._session.execute(
                select(Trace)
                .where(Trace.user_id == user_id)
                .order_by(Trace.started_at.desc())
                .limit(limit)
            ).scalars()
        )

    def list_for_session(
        self, session_id: str, *, user_id: str, limit: int = DEFAULT_LIST_LIMIT
    ) -> Sequence[Trace]:
        """One conversation's runs, newest first.

        Scoped by ``user_id`` as well as by session: the session id is a client-supplied value, so
        a guessable one must not become a way to read someone else's activity.
        """
        return tuple(
            self._session.execute(
                select(Trace)
                .where(Trace.session_id == session_id, Trace.user_id == user_id)
                .order_by(Trace.started_at.desc())
                .limit(limit)
            ).scalars()
        )

    def list_failures(self, *, user_id: str, limit: int = DEFAULT_LIST_LIMIT) -> Sequence[Trace]:
        """This user's failed runs, newest first.

        Worth a dedicated method rather than a filter argument: "what has been going wrong" is
        asked of the whole account rather than of one conversation, and it is the query an operator
        runs first.
        """
        return tuple(
            self._session.execute(
                select(Trace)
                .where(Trace.user_id == user_id, Trace.status == "error")
                .order_by(Trace.started_at.desc())
                .limit(limit)
            ).scalars()
        )

    def count_since(self, *, user_id: str, since: datetime, status: str | None = None) -> int:
        """How many runs this user started at or after ``since``.

        Counted in SQL rather than by loading rows and taking their length, so an operator asking a
        rate question does not pay for the rows they are only counting.

        ``status`` is validated the same way ``record`` validates it, for the same reason: an
        unrecognised value would silently match nothing and be reported as zero failures.
        """
        query = (
            select(func.count())
            .select_from(Trace)
            .where(Trace.user_id == user_id, Trace.started_at >= since)
        )
        if status is not None:
            if status not in STATUSES:
                msg = f"status must be one of {STATUSES}, got {status!r}"
                raise ValueError(msg)
            query = query.where(Trace.status == status)

        return self._session.execute(query).scalar_one()
