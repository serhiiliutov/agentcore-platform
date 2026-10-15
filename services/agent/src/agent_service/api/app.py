"""The FastAPI application: the AgentCore Runtime contract and its wiring.

Two routes are the contract. ``GET /ping`` answers health, and ``POST /invocations`` runs one
turn. Everything the web UI needs lives in a separate router — the page at ``/`` and the one
listing under ``/api`` — so a change to a local convenience endpoint cannot alter the contract's
shape.

The page is served from this same app rather than from a second service. It is a client of
``/invocations`` and the decision route exactly as any other client is, so a separate process
would add a container, a port and a CORS decision in exchange for nothing.

**The loop is injected, not imported.** ``create_app`` takes an ``InvocationHandler`` and the
default one reports that the loop is not installed. That is deliberate rather than a
placeholder to be replaced later: it means this module can be finished, tested and reviewed
— the routes, the health semantics, the error envelope, the session-header handling — while
the orchestration in Stage 8 arrives as a dependency rather than as an edit to the HTTP
layer. Nothing here has to change when it does, and nothing here pretends to work before it
does.

**The session header is honoured.** The AgentCore Runtime passes a session id as a header;
a plain client passes it in the body. Both are accepted and the header wins, because a
platform that sets it means it. Mapping that here — rather than in the loop — keeps the
conversation-continuation rule in the layer that receives the request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from starlette.types import Lifespan

from agent_service.config import Settings, get_settings

from .approval_routes import DecisionHandler, create_approval_router
from .cancel_routes import CancelHandler, create_cancel_router
from .models import (
    HealthResponse,
    InvocationRequest,
    InvocationResponse,
)
from .ui_routes import (
    ChooseProvider,
    DescribeProviders,
    PendingApprovalListing,
    SessionListing,
    TranscriptReader,
    create_ui_router,
)

#: The header the AgentCore Runtime uses to identify a conversation.
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

#: A handler turns one request into one response. Supplied by Stage 8.
InvocationHandler = Callable[[InvocationRequest], Awaitable[InvocationResponse]]


class InvocationNotConfiguredError(RuntimeError):
    """Raised by the default handler because the orchestration loop is not installed.

    A distinct type so the route can answer 501 Not Implemented — "this service is built but
    this endpoint is not wired" — rather than 500, which would claim a bug where there is a
    deliberate absence.
    """


async def _unconfigured_handler(_request: InvocationRequest) -> InvocationResponse:
    raise InvocationNotConfiguredError(
        "no invocation handler is installed; the orchestration loop is supplied at "
        "application construction time"
    )


class _InflightCounter:
    """Counts invocations in progress, so ``/ping`` can report ``HealthyBusy``.

    A plain integer guarded by the event loop's single-threaded execution: the increments
    and decrements happen between awaits, so no lock is needed, and adding one would suggest
    a concurrency the caller does not have.
    """

    def __init__(self) -> None:
        self._count = 0

    def __enter__(self) -> None:
        self._count += 1

    def __exit__(self, *_exc_info: object) -> None:
        self._count -= 1

    @property
    def busy(self) -> bool:
        return self._count > 0


def create_app(
    settings: Settings | None = None,
    *,
    invocation_handler: InvocationHandler | None = None,
    decision_handler: DecisionHandler | None = None,
    pending_approvals: PendingApprovalListing | None = None,
    session_listing: SessionListing | None = None,
    transcript_reader: TranscriptReader | None = None,
    cancel_handler: CancelHandler | None = None,
    describe_providers: DescribeProviders | None = None,
    choose_provider: ChooseProvider | None = None,
    lifespan: Lifespan[Any] | None = None,
) -> FastAPI:
    """Build the application.

    Every collaborator is injectable so a test can drive the contract without a database, an MCP
    server or a model provider, and so the loop, the gate and the MCP connection can be installed
    without this module importing any of them. ``lifespan`` is passed straight through to
    FastAPI for the same reason.

    The decision router is included unconditionally: with no handler it answers 501 per the
    default in `create_approval_router`, which keeps the route's existence visible in the
    OpenAPI-less surface rather than making it a 404 that looks like a typo.
    """
    resolved = settings or get_settings()
    handler = invocation_handler or _unconfigured_handler
    inflight = _InflightCounter()
    decisions = create_approval_router(decision_handler)
    cancels = create_cancel_router(cancel_handler)
    ui = create_ui_router(
        pending_approvals,
        describe_providers=describe_providers,
        choose_provider=choose_provider,
        list_sessions=session_listing,
        read_transcript=transcript_reader,
    )

    app = FastAPI(
        title="agent-service",
        version="0.1.0",
        # The contract is the documented surface; the generated schema is not it.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # Supplied by the composition root. It exists so a connection that has to be opened once
        # for the process — the MCP session — has a lifetime that is neither "per request" nor
        # "at import". ``None`` keeps the app constructible with no lifespan at all, which is what
        # the contract tests rely on.
        lifespan=lifespan,
    )

    app.state.settings = resolved
    app.state.inflight = inflight

    app.include_router(decisions)
    app.include_router(cancels)
    app.include_router(ui)

    @app.get("/ping", response_model=HealthResponse)
    async def ping() -> HealthResponse:
        """The AgentCore health contract.

        ``HealthyBusy`` while a turn is running is not a failure. The runtime keeps routing
        to a busy instance, and reporting ``Healthy`` under load would hide a saturated
        service from the supervisor that is supposed to notice.
        """
        return HealthResponse(status="HealthyBusy" if inflight.busy else "Healthy")

    @app.post("/invocations", response_model=InvocationResponse)
    async def invocations(payload: InvocationRequest, request: Request) -> Any:
        """Run one turn.

        A malformed body is rejected by FastAPI with a 422 before this function runs, so
        anything reaching here has a prompt and a ``user_id`` — which is what makes the
        user-scoped reads downstream safe.
        """
        session_id = request.headers.get(SESSION_HEADER) or payload.session_id
        if session_id:
            # Reflect the resolved id into the payload rather than passing two things to
            # the handler; the handler should not have to know a header exists.
            payload = payload.model_copy(update={"session_id": session_id})

        with inflight:
            try:
                return await handler(payload)
            except InvocationNotConfiguredError as exc:
                return JSONResponse(
                    status_code=status.HTTP_501_NOT_IMPLEMENTED,
                    content={"error": {"kind": "NotImplemented", "detail": str(exc)}},
                )

    return app
