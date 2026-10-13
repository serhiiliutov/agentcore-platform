"""Durable facts about a person: remembering, recalling, and expiring.

Three properties, and each is a decision rather than a mechanism.

**Remembering is an update, not an append.** ``(user_id, key)`` is unique, so writing the same
key again replaces the value. The alternative — storing every version and picking one at read
time — is how a memory feature becomes a source of contradictory answers, and the bug is
invisible because each individual read looks correct.

**Recall filters by expiry, and never deletes.** An expired fact is still a fact that was once
believed; keeping the row is what lets a reviewer see what the agent thought and when it
stopped applying. ``purge_expired`` exists for an operator tidying up, and is deliberately not
called on the read path — a read that also wrote would make "recall" a surprising thing to
call in a request handler.

**Every read takes a ``user_id``.** There is no unscoped variant and no default, so a caller
cannot read another person's facts by omission. Stage 2 put the column in the schema; this puts
the requirement where a mistake would otherwise be silent.

The value is a string, not a blob, because the schema says so and this respects it: if a piece
of information cannot be named, it does not belong in memory. That is what keeps the store
inspectable with one ``SELECT``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from agent_service.db.base import utc_now
from agent_service.db.models.memory import UserFact

#: Default lifetime in days when a caller does not say. A year is long enough to be useful and
#: short enough that a stale fact about someone's team or role cannot mislead indefinitely.
DEFAULT_TTL_DAYS = 365


class FactRepository:
    """Reads and writes over ``user_facts`` for one user."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def remember(
        self,
        *,
        user_id: str,
        key: str,
        value: str,
        source_session_id: str | None = None,
        ttl_days: int = DEFAULT_TTL_DAYS,
        now: datetime | None = None,
    ) -> UserFact:
        """Store or replace one fact, and refresh its expiry.

        Replacing rather than appending is the whole design: a key names one piece of
        information, so there is one current answer. ``source_session_id`` and ``created_at``
        are preserved on an update, because they record where the fact came from originally and
        rewriting them would lose the provenance the schema went to the trouble of keeping.
        """
        moment = now or utc_now()
        expires = moment + timedelta(days=ttl_days)

        existing = self._find(user_id=user_id, key=key)
        if existing is not None:
            existing.value = value
            existing.expires_at = expires
            # Provenance is only overwritten when the caller supplies a new source; otherwise
            # the fact keeps the conversation that first taught it.
            if source_session_id is not None:
                existing.source_session_id = source_session_id
            self._session.flush()
            return existing

        created = UserFact(
            user_id=user_id,
            key=key,
            value=value,
            source_session_id=source_session_id,
            created_at=moment,
            expires_at=expires,
        )
        self._session.add(created)
        self._session.flush()
        return created

    def recall(self, *, user_id: str, now: datetime | None = None) -> Mapping[str, str]:
        """Every current fact for this user, keyed by name.

        A mapping rather than a list, because the only use is "what is the value of X" and a
        list would push a linear scan into every caller.
        """
        moment = now or utc_now()
        rows = self._session.execute(
            select(UserFact).where(
                UserFact.user_id == user_id,
                UserFact.expires_at > moment,
            )
        ).scalars()

        return {row.key: row.value for row in rows}

    def recall_one(self, *, user_id: str, key: str, now: datetime | None = None) -> str | None:
        """One current fact's value, or ``None`` if it is absent or expired.

        Absent and expired are the same answer on purpose: a caller asking what it knows should
        not have to distinguish "never knew" from "no longer true".
        """
        moment = now or utc_now()
        row = self._session.execute(
            select(UserFact).where(
                UserFact.user_id == user_id,
                UserFact.key == key,
                UserFact.expires_at > moment,
            )
        ).scalar_one_or_none()

        return row.value if row is not None else None

    def forget(self, *, user_id: str, key: str) -> bool:
        """Delete one fact permanently. Returns whether anything was removed.

        A hard delete, unlike expiry, because this is a person asking to be forgotten rather
        than time passing — and answering that request with a hidden row would be the wrong
        reading of it.
        """
        ids = list(
            self._session.execute(
                select(UserFact.id).where(UserFact.user_id == user_id, UserFact.key == key)
            ).scalars()
        )
        if not ids:
            return False

        self._session.execute(delete(UserFact).where(UserFact.id.in_(ids)))
        self._session.flush()
        return True

    def all_for_user(self, *, user_id: str) -> tuple[UserFact, ...]:
        """Every fact for this user, expired ones included, newest first.

        For a reviewer answering "what has this system been told about me", which is a
        different question from ``recall`` and needs the expired rows.
        """
        return tuple(
            self._session.execute(
                select(UserFact)
                .where(UserFact.user_id == user_id)
                .order_by(UserFact.created_at.desc())
            ).scalars()
        )

    def purge_expired(self, *, now: datetime | None = None) -> int:
        """Delete every expired fact. Returns how many were removed.

        Not called anywhere on the request path on purpose — see the module docstring. It is an
        operator action, and separating it from reads keeps expiry a filter rather than a
        side effect.
        """
        moment = now or utc_now()
        ids = list(
            self._session.execute(
                select(UserFact.id).where(UserFact.expires_at <= moment)
            ).scalars()
        )
        if not ids:
            return 0

        self._session.execute(delete(UserFact).where(UserFact.id.in_(ids)))
        self._session.flush()
        return len(ids)

    def _find(self, *, user_id: str, key: str) -> UserFact | None:
        """The row for this key regardless of expiry, so an update can revive it."""
        return self._session.execute(
            select(UserFact).where(UserFact.user_id == user_id, UserFact.key == key)
        ).scalar_one_or_none()
