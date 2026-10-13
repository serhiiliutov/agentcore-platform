"""An idempotency ledger, because the ticketing API does not have one.

Stage 1 proved this directly: posting the same ticket twice creates two tickets, and
nothing in either response distinguishes a retry from a genuine second request. So the
only way to make a write safe to repeat is to make repetition unnecessary — record what
was done, and answer the repeat from the record.

The problem this solves is narrow and specific. `retry.py` will not retry a timeout,
because a timeout is compatible with the write having succeeded. That leaves the caller
holding an unanswered write. This module is how that question gets answered.

**The ledger is claimed before the call and completed after it.** That ordering is the
whole mechanism:

1. ``reserve`` atomically claims the key. If the key is already claimed, this is a
   repeat and the recorded outcome (or an in-progress marker) is returned instead of
   calling the API again.
2. The caller performs the write.
3. ``complete`` records the response, or ``release`` gives the claim back if the write
   provably did not happen.

Atomicity comes from the storage, not from the caller: the SQLite implementation claims
by INSERT, and a unique-key violation *is* the "already claimed" signal. A check-then-set
in Python would have a window where two concurrent retries both decide they are first,
which is precisely the bug the ledger exists to prevent.

**A claim that is never completed is not silently reused.** If a process dies mid-write,
the claim is left ``in_progress``. Reserving it again reports that back rather than
proceeding, because "the previous attempt's fate is unknown" and "no attempt was made"
are different answers and only one of them is safe to act on.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

#: How long a completed entry is kept, so a delayed retry still matches it.
DEFAULT_TTL_S: int = 24 * 60 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency (
    scope        TEXT NOT NULL,
    key          TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    state        TEXT NOT NULL,
    response     TEXT,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);
CREATE INDEX IF NOT EXISTS ix_idempotency_expires_at ON idempotency (expires_at);
"""


class ClaimState(StrEnum):
    """What happened when a key was reserved."""

    #: Nobody had this key. The caller owns the write and must complete or release it.
    CLAIMED = "claimed"
    #: This exact request already succeeded; ``response`` holds what to return.
    REPLAYED = "replayed"
    #: The key is claimed but has no recorded outcome. A previous attempt's fate is
    #: unknown — the caller must verify against the backend rather than assume.
    IN_PROGRESS = "in_progress"
    #: The key was used for a *different* request. A client bug worth surfacing.
    CONFLICT = "conflict"


@dataclass(frozen=True)
class Claim:
    """The result of reserving a key."""

    state: ClaimState
    response: dict[str, Any] | None = None


def fingerprint(payload: Any) -> str:
    """A stable hash of a request payload.

    Sorted keys and a fixed separator so that two dicts with the same content in a
    different insertion order produce the same fingerprint. Without that, a reused key
    with an identical request would be reported as a conflict, which is the opposite of
    the truth.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyLedger:
    """The ledger itself, backed by SQLite.

    SQLite rather than an in-memory dict for one reason: the claim has to survive the
    process that made it. An in-memory ledger loses exactly the entries that matter when
    the process crashes mid-write, which is the case it exists to handle.
    """

    def __init__(
        self, database_path: str | Path = ":memory:", *, ttl_s: int = DEFAULT_TTL_S
    ) -> None:
        self._ttl_s = ttl_s
        self._connection = sqlite3.connect(str(database_path))
        self._connection.row_factory = sqlite3.Row
        # WAL so a reader (another tool call) is not blocked by the writer mid-claim.
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        cursor = self._connection.cursor()
        try:
            yield cursor
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        finally:
            cursor.close()

    def reserve(self, scope: str, key: str, *, request_payload: Any) -> Claim:
        """Attempt to claim ``key`` for ``scope``.

        The INSERT is the lock. If it succeeds, this caller owns the write. If it fails
        on the primary key, someone else got there first and the stored row says whether
        that attempt finished, is still running, or was for a different request.
        """
        print_ = fingerprint(request_payload)
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=self._ttl_s)

        with self._cursor() as cursor:
            try:
                cursor.execute(
                    "INSERT INTO idempotency "
                    "(scope, key, fingerprint, state, response, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, NULL, ?, ?)",
                    (
                        scope,
                        key,
                        print_,
                        ClaimState.CLAIMED.value,
                        now.isoformat(),
                        expires.isoformat(),
                    ),
                )
                return Claim(state=ClaimState.CLAIMED)
            except sqlite3.IntegrityError:
                pass  # already present; fall through and read the existing row

            row = cursor.execute(
                "SELECT fingerprint, state, response, expires_at FROM idempotency "
                "WHERE scope = ? AND key = ?",
                (scope, key),
            ).fetchone()

        if row is None:  # pragma: no cover - only reachable if the row vanished
            return Claim(state=ClaimState.CLAIMED)

        # A reused key with different content is a caller mistake. Saying so is much
        # more useful than replaying the previous response to the wrong question.
        if row["fingerprint"] != print_:
            return Claim(state=ClaimState.CONFLICT)

        if row["state"] == ClaimState.CLAIMED.value:
            return Claim(state=ClaimState.IN_PROGRESS)

        if str(row["expires_at"]) < now.isoformat():
            # Expired: treat as absent, and take the claim for this attempt.
            self._delete(scope, key)
            return self.reserve(scope, key, request_payload=request_payload)

        stored = row["response"]
        return Claim(
            state=ClaimState.REPLAYED,
            response=json.loads(stored) if stored is not None else None,
        )

    def complete(self, scope: str, key: str, response: dict[str, Any]) -> None:
        """Record the outcome of a claimed write, making it replayable."""
        with self._cursor() as cursor:
            cursor.execute(
                "UPDATE idempotency SET state = ?, response = ? WHERE scope = ? AND key = ?",
                ("completed", json.dumps(response, default=str), scope, key),
            )

    def release(self, scope: str, key: str) -> None:
        """Give a claim back after a write that provably did not happen.

        Used when the failure is provably pre-handler — a 429 or 500 from the mock's own
        middleware. Retrying is then correct and must not be blocked by a stale claim.
        Deliberately *not* used for a timeout: that is the case where the write may have
        succeeded, and dropping the claim would allow the duplicate this ledger exists
        to prevent.
        """
        self._delete(scope, key)

    def _delete(self, scope: str, key: str) -> None:
        with self._cursor() as cursor:
            cursor.execute("DELETE FROM idempotency WHERE scope = ? AND key = ?", (scope, key))

    def purge_expired(self) -> int:
        """Remove entries past their TTL. Returns how many were removed."""
        now = datetime.now(UTC).isoformat()
        with self._cursor() as cursor:
            cursor.execute("DELETE FROM idempotency WHERE expires_at < ?", (now,))
            return cursor.rowcount

    def close(self) -> None:
        self._connection.close()
