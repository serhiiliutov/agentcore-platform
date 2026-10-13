"""The approval decision endpoints.

The loop proposes a write and stops; this is how a human answers. Kept in its own router,
like the future ``/api`` surface, so a change to a convenience endpoint cannot alter the
Runtime contract's shape — and included by `create_app` only when a handler is installed, so
the service still starts without one.

Three decisions are encoded here rather than left to a caller.

**Approving executes.** The route does not merely flip a state and return; it carries out the
write and reports what happened. That is what the person clicking "approve" means — a second
call asking them to confirm the thing they just confirmed would be friction with no safety
value, since the approval row is what authorises the write either way. The repository's
transition guard still applies: a stale or already-decided row is refused, not executed.

**Declining records and stops.** No execution follows a refusal, and the row is terminal, so
the same proposal cannot be approved later by anyone.

**The user is required, as on /invocations.** A decision is an action on user-owned data, so
it carries a ``user_id`` and the repository scopes every read by it. A decision for a proposal
that belongs to someone else is indistinguishable from one for an id that does not exist.

The handler is injected exactly as the invocation handler is, and the default answers 501. That
keeps this module testable without a database and keeps "not wired" distinguishable from
"broken".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

USER_ID_FIELD = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class ApprovalDecision(BaseModel):
    """A human's answer to one proposal."""

    model_config = ConfigDict(extra="ignore")

    #: Who is deciding. Required, and used to scope the lookup — see the module docstring.
    user_id: USER_ID_FIELD
    #: ``True`` approves and executes; ``False`` declines and stops.
    approved: bool


class ApprovalDecisionResult(BaseModel):
    """What came of the decision.

    ``state`` is the row's state *after* the decision, and ``outcome`` describes execution
    when there was one. They are separate because an approval that executed successfully and
    an approval whose execution was refused by the backend both end in a decided state, and
    only the second is a failure the user needs to know about.
    """

    approval_id: str
    action: str
    #: One of the model's ``ApprovalState`` values.
    state: str
    #: ``executed``, ``failed``, ``declined`` — or ``replayed`` is reported in ``detail``.
    outcome: str
    #: One sentence for the person who decided, written for a reader.
    detail: str = ""
    #: Whether the backend reported the write as already done, so the caller does not claim
    #: a new ticket was created.
    replayed: bool = False

    @property
    def succeeded(self) -> bool:
        return self.outcome == "executed"


#: A handler performs a decision. Supplied by the wiring layer; see `create_app`.
DecisionHandler = Callable[[str, str, bool], Awaitable[ApprovalDecisionResult]]


class DecisionNotConfiguredError(RuntimeError):
    """Raised by the default handler because the gate is not wired.

    Distinct from the invocation equivalent only in wording; both exist so the route answers
    501 rather than 500.
    """


async def _unconfigured_decision(
    _approval_id: str, _user_id: str, _approved: bool
) -> ApprovalDecisionResult:
    raise DecisionNotConfiguredError(
        "no approval decision handler is installed; the gate is supplied at application "
        "construction time"
    )


def create_approval_router(handler: DecisionHandler | None = None) -> APIRouter:
    """Build the router. A test passes a scripted handler; production passes the real one."""
    resolved = handler or _unconfigured_decision
    router = APIRouter(prefix="/invocations/approvals", tags=["approvals"])

    @router.post("/{approval_id}/decide", response_model=ApprovalDecisionResult)
    async def decide(approval_id: str, payload: ApprovalDecision) -> object:
        """Answer a proposal.

        A malformed body is rejected with a 422 before this runs, so anything reaching here
        has a ``user_id`` and a decision — which is what makes the scoped lookup safe.

        A refusal by the transition guard is returned as a 409 rather than a 400: the request
        is well-formed and the caller is entitled to ask; the *state* is what prevents it, and
        that is a conflict rather than a bad request.
        """
        try:
            return await resolved(approval_id, payload.user_id, payload.approved)
        except DecisionNotConfiguredError as exc:
            return JSONResponse(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                content={"error": {"kind": "NotImplemented", "detail": str(exc)}},
            )
        except DecisionConflictError as exc:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"error": {"kind": "Conflict", "detail": str(exc)}},
            )

    return router


class DecisionConflictError(RuntimeError):
    """The proposal cannot be decided in its current state.

    Raised by the wiring layer when the repository's transition guard refuses — the row is
    already decided, already expired, or not this user's. The route turns it into a 409.
    """
