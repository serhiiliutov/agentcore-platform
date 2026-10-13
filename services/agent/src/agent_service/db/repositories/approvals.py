"""The approval repository: proposing writes, and deciding them at most once.

This is where the assignment's central guarantee is enforced in code. A write the model
proposed exists as a row, and that row is the *only* thing that can authorise the call. Three
properties make that structural rather than aspirational:

**The idempotency key is minted at proposal time, not execution time.** ``client_request_id``
is generated in ``propose`` and stored on the row. Re-executing an approved action after a
restart therefore reuses the same key, and Stage 4's ledger turns the second attempt into a
replay instead of a second ticket. A key minted at execution would be new on every attempt and
would defeat the guarantee entirely.

**The transition guard lives here, not in the caller.** ``decide`` refuses anything that is not
``pending``, and refuses a proposal whose time has passed. Every caller — the HTTP route, a
test, a future CLI — gets that behaviour without having to remember it, which is the difference
between a rule and a convention.

**User-scoping is a parameter, not a filter.** Every read takes a ``user_id``, so a caller
cannot accidentally read another user's proposal by omitting a clause. Stage 2 put the column
in the schema; this puts the requirement at the call site.

One implementation note that is not cosmetic: SQLite does not preserve ``tzinfo``, so a
timestamp read back is naive while ``utc_now()`` is aware, and comparing the two raises
``TypeError`` at runtime with nothing the type checker could have caught. Every comparison goes
through ``_naive`` for that reason.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_service.db.base import utc_now
from agent_service.db.models.approval import TERMINAL_STATES, Approval, ApprovalState

#: Default lifetime of a proposal when the caller does not say. An approval is a decision
#: about *now*; a proposal left overnight should not be executable the next morning.
DEFAULT_TTL_SECONDS = 1800


class ApprovalTransitionError(RuntimeError):
    """A state change was attempted that the lifecycle does not allow.

    Raised for a decision on an already-decided proposal, or an execution of one that was never
    approved. The database's check constraint catches an invalid *value*; this catches an
    invalid *transition*, which a constraint cannot express.
    """


def _naive(value: datetime) -> datetime:
    """Normalise to naive UTC so a comparison cannot raise on mixed awareness.

    SQLite hands back naive timestamps for a ``DateTime(timezone=True)`` column, so comparing
    one against ``utc_now()`` would raise ``TypeError`` — a runtime failure in a code path the
    type checker sees as fine.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


class ApprovalRepository:
    """Reads and writes over the ``approvals`` table for one session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def propose(
        self,
        *,
        session_id: str,
        user_id: str,
        action: str,
        payload: dict[str, Any],
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        client_request_id: str | None = None,
        now: datetime | None = None,
    ) -> Approval:
        """Record a proposed write as ``pending``.

        ``client_request_id`` may be supplied so a caller that already has a key — a retried
        proposal, or a scripted run with a fixed one — reuses it. Otherwise a fresh key is
        minted here, which is the point: the key exists before anyone approves anything, so the
        eventual execution is replay-safe by construction.
        """
        moment = now or utc_now()
        key = client_request_id or f"appr-{uuid.uuid4().hex}"
        if client_request_id is not None and self._key_is_taken(client_request_id):
            key = f"appr-{uuid.uuid4().hex}"

        approval = Approval(
            id=str(uuid.uuid4()),
            session_id=session_id,
            user_id=user_id,
            action=action,
            payload=payload,
            state=ApprovalState.PENDING.value,
            client_request_id=key,
            requested_at=moment,
            expires_at=moment + timedelta(seconds=ttl_seconds),
        )
        self._session.add(approval)
        self._session.flush()
        return approval

    def _key_is_taken(self, client_request_id: str) -> bool:
        """Whether a proposal already holds this idempotency key.

        The column is globally unique because a key names one write, and a second proposal
        carrying the same key used to fail the insert outright -- which the HTTP layer reported
        as a 500. The scripted provider's key is fixed, so that happened on every run after the
        first and made the demo unusable.

        Not scoped by user or session: the constraint is the column's, so a narrower question
        would let two rows collide anyway and answer wrongly.
        """
        found = self._session.execute(
            select(Approval.id).where(Approval.client_request_id == client_request_id).limit(1)
        ).first()
        return found is not None

    def get(self, approval_id: str, *, user_id: str) -> Approval | None:
        """Fetch one proposal, or ``None`` if it is not this user's.

        Returns ``None`` rather than raising for a foreign row, so a caller cannot use the
        difference between "does not exist" and "not yours" to probe for other users'
        identifiers.
        """
        return self._session.execute(
            select(Approval).where(Approval.id == approval_id, Approval.user_id == user_id)
        ).scalar_one_or_none()

    def pending_for_user(self, *, user_id: str) -> tuple[Approval, ...]:
        """Every still-decidable proposal for this user, oldest first.

        The user-scoped counterpart of ``pending`` below, and it exists because of the durability
        requirement rather than for symmetry. A proposal must survive a restart of the service.
        ``pending`` is keyed by session, so a page that knows only the user cannot ask which
        session an outstanding proposal belongs to — and a proposal nobody can find is one nobody
        can approve. The guarantee would hold in the database and be invisible in the one place a
        human looks for it.

        Ordered oldest first, like ``pending``: the proposal that has been waiting longest is the
        one most likely to expire before anyone acts on it.

        Expired-but-unswept rows are filtered out here for the same reason they are there — a
        caller must never be handed something it cannot decide.
        """
        moment = _naive(utc_now())
        rows = self._session.execute(
            select(Approval)
            .where(
                Approval.user_id == user_id,
                Approval.state == ApprovalState.PENDING.value,
            )
            .order_by(Approval.requested_at)
        ).scalars()

        return tuple(row for row in rows if _naive(row.expires_at) > moment)

    def pending(self, *, session_id: str, user_id: str) -> tuple[Approval, ...]:
        """Every still-decidable proposal for this session, oldest first.

        The hot query — it is what the loop asks before starting a turn, so it is shaped to
        hit ``ix_approvals_session_state``. Proposals that have expired but not yet been swept
        are excluded, because a caller must never be handed something it cannot decide.
        """
        moment = _naive(utc_now())
        rows = self._session.execute(
            select(Approval)
            .where(
                Approval.session_id == session_id,
                Approval.user_id == user_id,
                Approval.state == ApprovalState.PENDING.value,
            )
            .order_by(Approval.requested_at)
        ).scalars()

        return tuple(row for row in rows if _naive(row.expires_at) > moment)

    def decide(
        self,
        approval_id: str,
        *,
        user_id: str,
        approved: bool,
        now: datetime | None = None,
    ) -> Approval:
        """Allow or refuse a proposal, exactly once.

        An expired proposal is marked ``expired`` and the decision refused rather than being
        quietly honoured — a stale approval executed late is worse than a refused one, because
        the ticket it creates may be pointless or actively confusing.
        """
        approval = self.get(approval_id, user_id=user_id)
        if approval is None:
            raise ApprovalTransitionError(f"no approval {approval_id!r} for this user")

        if approval.state != ApprovalState.PENDING.value:
            raise ApprovalTransitionError(
                f"approval {approval_id!r} is {approval.state!r} and cannot be decided again"
            )

        moment = now or utc_now()
        if _naive(approval.expires_at) <= _naive(moment):
            approval.state = ApprovalState.EXPIRED.value
            approval.decided_at = moment
            self._session.flush()
            raise ApprovalTransitionError(f"approval {approval_id!r} expired before it was decided")

        approval.state = ApprovalState.APPROVED.value if approved else ApprovalState.DECLINED.value
        approval.decided_at = moment
        self._session.flush()
        return approval

    def claim_for_execution(
        self, approval_id: str, *, user_id: str, now: datetime | None = None
    ) -> Approval:
        """Return an approved proposal that is ready to execute.

        Refuses anything but ``approved``: an execution of a ``pending`` row would be exactly
        the write-without-consent this table exists to prevent, and one of a ``declined`` row
        would be worse.
        """
        approval = self.get(approval_id, user_id=user_id)
        if approval is None:
            raise ApprovalTransitionError(f"no approval {approval_id!r} for this user")

        if approval.state == ApprovalState.PENDING.value:
            raise ApprovalTransitionError(
                f"approval {approval_id!r} has not been decided; it cannot be executed"
            )
        if approval.state != ApprovalState.APPROVED.value:
            raise ApprovalTransitionError(
                f"approval {approval_id!r} is {approval.state!r}; only an approved one may run"
            )
        return approval

    def mark_executed(
        self,
        approval_id: str,
        *,
        result: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> Approval:
        """Record that the write succeeded, with what it returned."""
        return self._finish(
            approval_id, state=ApprovalState.EXECUTED, result=result, error=None, now=now
        )

    def mark_failed(
        self,
        approval_id: str,
        *,
        error: str,
        now: datetime | None = None,
    ) -> Approval:
        """Record that the write was attempted and refused.

        A distinct terminal state from ``declined``: one is a human saying no, the other is the
        backend saying no, and an operator debugging a missing ticket needs to tell them apart.
        """
        return self._finish(
            approval_id, state=ApprovalState.FAILED, result=None, error=error, now=now
        )

    def expire_stale(self, *, now: datetime | None = None) -> int:
        """Mark every overdue pending proposal ``expired``. Returns how many changed.

        ``pending`` already hides overdue rows, so this is not what makes a stale approval
        unexecutable — it is what stops one from lingering as ``pending`` forever and being
        counted as outstanding work by an operator or a dashboard.
        """
        moment = now or utc_now()
        rows = self._session.execute(
            select(Approval).where(Approval.state == ApprovalState.PENDING.value)
        ).scalars()

        changed = 0
        for row in rows:
            if _naive(row.expires_at) <= _naive(moment):
                row.state = ApprovalState.EXPIRED.value
                row.decided_at = moment
                changed += 1
        if changed:
            self._session.flush()
        return changed

    def _finish(
        self,
        approval_id: str,
        *,
        state: ApprovalState,
        result: dict[str, Any] | None,
        error: str | None,
        now: datetime | None,
    ) -> Approval:
        """Shared tail of the two terminal transitions.

        Both require ``approved`` as the starting point, so neither can be reached from
        ``pending`` or from an already-terminal row.
        """
        approval = self._by_id(approval_id)
        if approval.state != ApprovalState.APPROVED.value:
            raise ApprovalTransitionError(
                f"approval {approval_id!r} is {approval.state!r}; "
                f"only an approved one can reach {state.value!r}"
            )

        approval.state = state.value
        approval.executed_at = now or utc_now()
        approval.result = result
        approval.error = error
        self._session.flush()
        return approval

    def _by_id(self, approval_id: str) -> Approval:
        """Internal lookup without the user clause.

        Used only from ``_finish``, which runs after ``claim_for_execution`` has already proved
        ownership. Kept separate and named plainly so the two are not confused.
        """
        approval = self._session.get(Approval, approval_id)
        if approval is None:
            raise ApprovalTransitionError(f"no approval {approval_id!r}")
        return approval


def is_terminal(state: str) -> bool:
    """Whether a state admits no further transition.

    Reads ``TERMINAL_STATES`` rather than restating the set, so adding a state to the model
    cannot leave this function behind.
    """
    try:
        return ApprovalState(state) in TERMINAL_STATES
    except ValueError:
        return False
