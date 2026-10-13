"""Cancelling a turn that is still running.

The loop stops at a checkpoint when its `CancelToken` is set (Stage 12), and the registry holds
the tokens for the runs this process has (``agent/inflight.py``). This is the endpoint that joins
them: the only way a person can say "stop" to something already in flight.

**Why the path names a session, not a request.** The plan called for
``POST /invocations/{request_id}/cancel``, and the AgentCore contract carries no request id — a
client is given a session id and nothing else. Naming a run id instead would mean widening the
documented response shape to support a local convenience, and that shape belongs to the platform.
A conversation has at most one turn in flight — a turn ends the request that started it — so "the
run in this conversation" is unambiguous.

**Why 404 rather than 204 when there is nothing to stop.** They describe different situations and
a client should be able to tell them apart: "there was a run and it is now stopping" is not the
same as "there was nothing running". Reporting both as success would make the Cancel button look
like it worked when the turn had already finished, and a UI that cannot tell would leave the user
believing they stopped something that had already run to completion.

**Why a cancel is not guaranteed to prevent an effect.** Cancellation is cooperative and is only
checked *between* steps, so a write already executing is allowed to finish and be recorded (see
``agent/budget.py``). The response says ``stopping`` rather than ``stopped`` for exactly this
reason: it is honest about what has been asked for, which is a request to stop rather than proof
that nothing further will happen. The alternative — claiming the turn is dead — would be a promise
this design deliberately does not make.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

USER_ID_FIELD = Annotated[str, StringConstraints(min_length=1, max_length=200)]
SESSION_ID_FIELD = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class CancelRequest(BaseModel):
    """Who is asking to stop. The user is required for the same reason it is on every other
    route: cancellation is an action on user-owned work, and the lookup is scoped by it."""

    model_config = ConfigDict(extra="ignore")

    user_id: USER_ID_FIELD


class CancelResult(BaseModel):
    """What came of the request.

    ``state`` is deliberately ``stopping`` rather than ``stopped``: cooperative cancellation means
    the run is asked to stop at its next checkpoint, so a step already in flight is still allowed
    to finish. See the module docstring.
    """

    session_id: str
    state: str
    #: One sentence for the person who clicked.
    detail: str = ""


#: Performs the cancellation: given a session and a user, either finds a run and asks it to stop,
#: or reports that there was nothing to stop. Supplied by the wiring layer.
CancelHandler = Callable[[str, str], Awaitable[CancelResult | None]]


class CancelNotConfiguredError(RuntimeError):
    """Raised by the default handler because no registry is installed."""


async def _unconfigured_cancel(_session_id: str, _user_id: str) -> CancelResult | None:
    raise CancelNotConfiguredError(
        "no cancel handler is installed; the in-flight registry is supplied at application "
        "construction time"
    )


def create_cancel_router(handler: CancelHandler | None = None) -> APIRouter:
    """Build the router. A test passes a scripted handler; production passes the real one."""
    resolved = handler or _unconfigured_cancel
    router = APIRouter(prefix="/invocations", tags=["cancel"])

    @router.post(
        "/{session_id}/cancel",
        response_model=CancelResult,
        responses={404: {"description": "no run is in flight for this conversation"}},
    )
    async def cancel(session_id: str, payload: CancelRequest) -> object:
        """Ask the turn running in this conversation to stop.

        A malformed body is rejected with a 422 before this runs, so anything reaching here has a
        ``user_id`` — which is what makes the scoped lookup safe.

        Nothing to stop is a 404, and a registry that is not wired is a 501. They are kept
        distinct so "not built" never looks like "nothing was running": the first is a deployment
        mistake and the second is a normal race with a turn that just finished.
        """
        try:
            found = await resolved(session_id, payload.user_id)
        except CancelNotConfiguredError as exc:
            return JSONResponse(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                content={"error": {"kind": "NotImplemented", "detail": str(exc)}},
            )

        if found is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={
                    "error": {
                        "kind": "NotFound",
                        "detail": (
                            "no turn is in flight for this conversation; it may have already "
                            "finished, or been started by a different user"
                        ),
                    }
                },
            )
        return found

    return router
