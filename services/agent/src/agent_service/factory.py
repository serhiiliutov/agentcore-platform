"""The composition root: the one place that knows how the parts fit together.

Every other module is a piece. This is where the pieces meet, and it exists because the
alternative — each module importing what it needs — would make the dependency graph a cycle and
would hide the assembly somewhere inside the HTTP layer.

**The schema is not created here.** Alembic owns it (ADR 0004). A ``create_all`` would produce a
database that matches the models by accident and diverges from the migrations the first time one
is added, and the divergence would appear in production rather than in review. A bare checkout
runs ``alembic upgrade head``; see the README.

**The MCP connection is opened once, for the process.** ``TicketingMcpClient.connect`` is an
async context manager: it starts the server, performs the initialize handshake and verifies the
tool catalog. Doing that per request would pay a process spawn and a handshake on every turn, and
doing it at import time would make an unreachable server a failure to *import* rather than a
failure to *serve*. So it lives in the app's lifespan, and the request handlers reach it through
the holder below.

**The handlers are built before the connection exists.** FastAPI constructs the app, then runs
the lifespan; the routes have to exist in between. The indirection that resolves is a small
holder with a deliberate, named failure for the case where a request arrives before startup
finished — which cannot normally happen, and is exactly the kind of impossibility that becomes a
confusing ``AttributeError`` when it quietly stops being true.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from agent_service.agent.execution import WriteExecutor
from agent_service.agent.gate import ApprovalGate
from agent_service.agent.handler import InvocationHandlerFactory, as_pending_approval
from agent_service.agent.loop import LoopLimits
from agent_service.api.app import InvocationHandler, create_app
from agent_service.api.approval_routes import DecisionConflictError, DecisionHandler
from agent_service.api.models import InvocationRequest, InvocationResponse, PendingApproval
from agent_service.api.ui_routes import PendingApprovalListing
from agent_service.config import Settings, get_settings
from agent_service.db.base import create_session_factory, register_sqlite_pragmas
from agent_service.db.repositories.approvals import (
    ApprovalRepository,
    ApprovalTransitionError,
)
from agent_service.mcp.client import TicketingMcpClient
from agent_service.providers.base import ModelProvider, build_provider
from agent_service.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

#: The tools the agent may call. Passed to the client so the loop and the registry agree on one
#: timeout rather than each carrying its own default.
TOOL_TIMEOUT_S = 30.0


class ServiceNotStartedError(RuntimeError):
    """A request arrived before the MCP connection was established.

    Named rather than left as an ``AttributeError`` on ``None``, so the one case where it can
    happen — a request racing startup — reports what actually went wrong.
    """


@dataclass
class Runtime:
    """What the handlers need once the process is serving.

    A mutable holder rather than constructor arguments because the connection it carries is only
    available from inside the app's lifespan, after ``create_app`` has already been called and the
    routes already built.
    """

    tools: ToolRegistry | None = None
    executor: Any | None = field(default=None)

    def require_tools(self) -> ToolRegistry:
        if self.tools is None:
            raise ServiceNotStartedError(
                "the MCP tool catalog is not available; the service is still starting up or "
                "the connection to the ticketing server failed"
            )
        return self.tools

    def require_executor(self) -> Any:
        if self.executor is None:
            raise ServiceNotStartedError(
                "the tool executor is not available; the service is still starting up or the "
                "connection to the ticketing server failed"
            )
        return self.executor


class McpToolExecutor:
    """The loop's ``ToolExecutor``, backed by the ticketing MCP client.

    An adapter rather than making the client implement the protocol: the client's ``call`` returns
    a structured result and takes an optional timeout, and the loop wants one string. That
    translation belongs here, at the seam, and not in either of the two things being joined.

    ``as_model_text`` is what the model sees. The structured form is kept on the result for the
    trace and for the approval path, so a failure that a user should read as a sentence can still
    be recorded as data.
    """

    def __init__(self, client: TicketingMcpClient, *, timeout_s: float = TOOL_TIMEOUT_S) -> None:
        self._client = client
        self._timeout_s = timeout_s

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        result = await self._client.call(name, arguments, timeout_s=self._timeout_s)
        return result.as_model_text()


def _sqlite_path(database_url: str) -> Path | None:
    """The file a SQLite URL points at, or ``None`` for an in-memory one.

    SQLite will not create a missing directory, and reports that as "unable to open database
    file" — which reads like a permissions problem and sends a reader looking in the wrong place.
    Creating it here turns a misleading failure into a non-event.
    """
    prefix = "sqlite+pysqlite:///"
    if not database_url.startswith(prefix):
        return None
    remainder = database_url[len(prefix) :]
    if not remainder or remainder.startswith(":memory:"):
        return None
    return Path(remainder)


def build_engine(settings: Settings) -> Engine:
    """Open the database engine the settings describe.

    SQLite gets ``check_same_thread=False`` because FastAPI runs handlers on a thread pool and
    the session may move between threads within one request; the pragmas set WAL and enforce
    foreign keys, which SQLite leaves off by default and which the approval foreign key depends
    on.

    A server database gets ``pool_pre_ping``: a connection that a proxy has closed between
    requests would otherwise fail the first query of a turn with a confusing driver error.
    """
    if settings.is_sqlite:
        directory = _sqlite_path(settings.database_url)
        if directory is not None:
            directory.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False},
            future=True,
        )
        register_sqlite_pragmas(engine)
        return engine

    return create_engine(settings.database_url, pool_pre_ping=True, future=True)


def build_invocation_handler(
    *,
    provider: ModelProvider,
    runtime: Runtime,
    sessions: sessionmaker[Session],
    settings: Settings,
) -> InvocationHandler:
    """Adapt the loop factory into the one-argument handler the route expects.

    Two mismatches are resolved here, and both are deliberate rather than incidental.

    **The session.** ``create_app``'s ``InvocationHandler`` takes only a request, because the HTTP
    layer should not know a database exists; ``InvocationHandlerFactory`` takes a request *and* a
    session, because the loop needs one. This function opens it, and owns the transaction
    boundary — so a turn is committed as a unit, and a failure part-way through leaves nothing
    half-written for the next turn to read.

    **The tools.** ``InvocationHandlerFactory`` is constructed per request rather than once,
    because the tool catalog comes from a connection that is only live between lifespan start and
    stop. The construction is a handful of attribute assignments — the expensive parts, the
    provider and the loop, are per-request anyway.

    The configured budgets are passed through here. Reading them from settings but then letting
    the loop fall back to its own defaults would make the settings decorative, and an operator
    who raised the step ceiling would find nothing changed.
    """
    limits = LoopLimits(
        max_steps=settings.agent_max_steps,
        approval_ttl_seconds=settings.agent_approval_ttl_s,
    )

    async def handle(request: InvocationRequest) -> InvocationResponse:
        factory = InvocationHandlerFactory(
            provider=provider,
            tools=runtime.require_tools(),
            executor=runtime.require_executor(),
            limits=limits,
            trace_dir=settings.trace_dir,
        )
        with sessions() as session:
            response = await factory(request, session)
            session.commit()
            return response

    return handle


def build_decision_handler(
    *,
    runtime: Runtime,
    sessions: sessionmaker[Session],
) -> DecisionHandler:
    """Adapt the approval gate into the decision handler the route expects.

    The gate is built per request because it is bound to one session — ``for_session`` says so in
    its name and its signature — and because the write it may perform must commit together with
    the decision that authorised it.

    **The transition error becomes the route's conflict error.** The repository refuses a decision
    on a row that is already decided, expired or not this user's, and raises
    ``ApprovalTransitionError``. The route turns ``DecisionConflictError`` into a 409; without this
    translation the same refusal would surface as a 500, telling a caller the service was broken
    when in fact their request was simply too late. Nothing is swallowed — the cause is chained.
    """

    async def decide(approval_id: str, user_id: str, approved: bool) -> Any:
        with sessions() as session:
            gate = ApprovalGate.for_session(
                session,
                executor=WriteExecutor(
                    executor=runtime.require_executor(),
                    approvals=ApprovalRepository(session),
                ),
            )
            try:
                result = await gate.decide(approval_id, user_id, approved)
            except ApprovalTransitionError as exc:
                raise DecisionConflictError(str(exc)) from exc
            session.commit()
            return result

    return decide


def build_pending_listing(
    *,
    sessions: sessionmaker[Session],
) -> PendingApprovalListing:
    """The listing the page fetches on load.

    Scoped by user and by nothing else, which is the whole point: the page does not know which
    session an outstanding proposal belongs to, and the requirement is that a proposal survives a
    restart — so "what is waiting for me" has to be answerable without one.

    Converts through the same ``as_pending_approval`` the invocation response uses, so a proposal
    reads identically whichever endpoint delivered it.
    """

    async def listing(user_id: str) -> Sequence[PendingApproval]:
        with sessions() as session:
            rows = ApprovalRepository(session).pending_for_user(user_id=user_id)
            return [as_pending_approval(row) for row in rows]

    return listing


def build_lifespan(settings: Settings, runtime: Runtime) -> Any:
    """Open the MCP connection for the life of the process.

    Entering the context manager starts the ticketing server, initialises the session and verifies
    the tool catalog against what the service expects. Doing this at startup means a misconfigured
    command or a missing server fails *before* the first request, with the startup log as the
    place to look — instead of surfacing inside a user's first turn as a tool error.

    The catalog is published to the runtime only once the connection is live, so a handler that
    somehow runs early gets ``ServiceNotStartedError`` rather than a half-built registry.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with TicketingMcpClient.connect(settings) as client:
            runtime.tools = ToolRegistry.from_discoverable(client)
            runtime.executor = McpToolExecutor(client)
            logger.info(
                "ticketing MCP connected; %d tool(s) available",
                len(runtime.tools),
            )
            yield

    return lifespan


def build_app(settings: Settings | None = None) -> FastAPI:
    """Assemble the runnable application.

    The order here is the dependency order, and it is the reason this function exists rather than
    each module reaching for what it needs: settings produce an engine, the engine produces
    sessions, the provider is chosen from settings, and the MCP connection — the one thing that
    cannot be built synchronously — arrives through the lifespan.

    ``get_settings`` is called once and the result passed down, so every part of the process reads
    the same values. Re-reading the environment per component is how two of them end up disagreeing
    about which database they are talking to.
    """
    resolved = settings or get_settings()
    engine = build_engine(resolved)
    sessions = create_session_factory(engine)
    provider = build_provider(resolved)
    runtime = Runtime()

    app = create_app(
        resolved,
        invocation_handler=build_invocation_handler(
            provider=provider, runtime=runtime, sessions=sessions, settings=resolved
        ),
        decision_handler=build_decision_handler(runtime=runtime, sessions=sessions),
        pending_approvals=build_pending_listing(sessions=sessions),
        lifespan=build_lifespan(resolved, runtime),
    )
    app.state.engine = engine
    app.state.runtime = runtime
    return app
