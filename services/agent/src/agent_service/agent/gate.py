"""The approval gate's entrypoint: decide, and execute only if approved.

This is the adapter between the decision *route* and the two components that already exist. It
holds almost no logic of its own, and that is the point: every rule it appears to apply is
enforced somewhere below it, by code that is tested without this file.

* whether a decision is legal — `ApprovalRepository.decide`, which refuses a non-pending row
  and marks an expired one;
* whether an execution is legal — `ApprovalRepository.claim_for_execution`, which refuses
  anything but ``approved``;
* what runs, and with which key — `WriteExecutor`, which sends the stored payload.

So this file's job is ordering and translation: order because approving must execute *after*
the decision is recorded, and translate because the route speaks
``ApprovalDecisionResult`` while the components below speak repository states and
`ExecutionResult`.

One rule *is* decided here, because it cannot live below: **declining does not execute.** The
repository has no opinion about that — it records a state — and the executor cannot run a
declined row. So the branch is here, and it is the only branch.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from agent_service.api.approval_routes import ApprovalDecisionResult, DecisionConflictError
from agent_service.db.models.approval import Approval, ApprovalState
from agent_service.db.repositories.approvals import (
    ApprovalRepository,
    ApprovalTransitionError,
)

from .execution import ExecutionResult, WriteExecutor

#: What a declined decision reports as its outcome. Distinct from ``failed`` on purpose: failed
#: means the backend refused the write, declined means a person did, and a user reading their
#: own history needs to tell "I said no" from "it did not work".
DECLINED_OUTCOME = "declined"


class ApprovalGate:
    """Answers a proposal, executing it only when it is approved."""

    def __init__(self, *, executor: WriteExecutor, approvals: ApprovalRepository) -> None:
        self._executor = executor
        self._approvals = approvals

    @classmethod
    def for_session(cls, session: Session, *, executor: WriteExecutor) -> ApprovalGate:
        """Build a gate bound to one database session.

        The executor is supplied rather than built because it holds the tool transport, which
        is per-process rather than per-request — rebuilding it here would create a new
        connection pool per decision.
        """
        return cls(executor=executor, approvals=ApprovalRepository(session))

    async def decide(
        self, approval_id: str, user_id: str, approved: bool
    ) -> ApprovalDecisionResult:
        """Record a decision, and carry out the write if it was an approval.

        The decision is recorded *before* the write is attempted, deliberately. If execution
        then fails, the row is ``approved`` and the executor marks it ``failed`` — a state a
        retry can reason about. Recording it after would leave a window in which a crash left
        the row ``pending`` and the write performed, which is the one state that cannot be
        distinguished from "nothing happened".
        """
        try:
            row = self._approvals.decide(approval_id, user_id=user_id, approved=approved)
        except ApprovalTransitionError as exc:
            # The guard refused: already decided, expired, or not this user's. Re-raised as
            # the route's own type so the HTTP layer maps it to a 409 without knowing what a
            # repository is.
            raise DecisionConflictError(str(exc)) from exc

        if not approved:
            return _declined(row)

        try:
            outcome = await self._executor.execute(approval_id, user_id=user_id)
        except ApprovalTransitionError as exc:
            # The decision was recorded but the execution was refused — a race in which the
            # row changed between the two calls. Reported as a conflict rather than swallowed,
            # because the caller's approval did not result in a write.
            raise DecisionConflictError(str(exc)) from exc

        return _executed(row, outcome)


def _declined(row: Approval) -> ApprovalDecisionResult:
    """The result of a refusal.

    Reads the state off the row rather than asserting ``declined``, so if the repository ever
    records something else — an expiry that raced the decision — the response reports what
    actually happened.
    """
    return ApprovalDecisionResult(
        approval_id=row.id,
        action=row.action,
        state=row.state,
        outcome=DECLINED_OUTCOME,
        detail="You declined this. Nothing was sent to the ticketing system.",
        replayed=False,
    )


def _executed(row: Approval, outcome: ExecutionResult) -> ApprovalDecisionResult:
    """The result of an approval.

    ``state`` is derived from the outcome rather than read back from the row. The executor has
    always moved the row by the time it returns — ``mark_executed`` on success, ``mark_failed``
    on a refusal — so the two agree, and a second query would only add a round trip. What must
    not happen is reporting the state the row held *before* execution: that would tell the user
    their approval is still outstanding when it is not.
    """
    detail = outcome.detail
    if outcome.outcome == "failed":
        detail = f"The write was attempted and the ticketing system refused it: {detail}"

    return ApprovalDecisionResult(
        approval_id=row.id,
        action=row.action,
        state=ApprovalState.EXECUTED.value if outcome.succeeded else ApprovalState.FAILED.value,
        outcome=outcome.outcome,
        detail=detail,
        replayed=outcome.replayed,
    )
