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

**There is one decision implementation, and two ways in.** A write is approved either by the
decision route — the page's button — or by the user's next message, which is how `CONTRACT.md`
says the platform does it. Both call the same callable built here; they differ only in whose
database session it runs in. Building it twice is how a service ends up with a documented flow and
an undocumented one that behave differently, with only one of them tested.
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

from agent_service.agent.execution import ToolCallFailedError, WriteExecutor
from agent_service.agent.gate import ApprovalGate
from agent_service.agent.handler import (
    DecisionApplier,
    InvocationHandlerFactory,
    as_pending_approval,
)
from agent_service.agent.inflight import InflightRegistry
from agent_service.agent.loop import LoopLimits, ToolOutcome
from agent_service.api.app import InvocationHandler, create_app
from agent_service.api.approval_routes import (
    ApprovalDecisionResult,
    DecisionConflictError,
    DecisionHandler,
)
from agent_service.api.cancel_routes import CancelHandler, CancelResult
from agent_service.api.models import InvocationRequest, InvocationResponse, PendingApproval
from agent_service.api.provider_control import ProviderControl
from agent_service.api.ui_routes import PendingApprovalListing
from agent_service.config import Settings, get_settings
from agent_service.db.base import create_session_factory, register_sqlite_pragmas
from agent_service.db.repositories.approvals import ApprovalRepository
from agent_service.db.repositories.traces import TraceRepository
from agent_service.mcp.client import TicketingMcpClient, ToolCallResult
from agent_service.observability import EXECUTION, Tracer
from agent_service.providers.registry import ProviderRegistry
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
    a structured result and takes an optional timeout, and the loop wants a `ToolOutcome`. That
    translation belongs here, at the seam, and not in either of the two things being joined.

    Both halves of the outcome are carried through, and the second one is the point.
    ``as_model_text`` is what the model sees, which is the text half. ``is_error`` is what the
    *caller* is told, and dropping it here — which is what this adapter used to do — meant the
    loop had no way to know a call had failed and so reported every call as executed. A refused
    search reached the response as a successful one, with the only record of the refusal being
    the sentence fed to the model.
    """

    def __init__(self, client: TicketingMcpClient, *, timeout_s: float = TOOL_TIMEOUT_S) -> None:
        self._client = client
        self._timeout_s = timeout_s

    @staticmethod
    def _replayed(result: ToolCallResult) -> bool:
        """Whether the backend answered "already done" rather than doing it again.

        Read from the structured payload, never from the sentence beside it. The write path used
        to search the text for the words "replayed" and "recovered", which works right up until
        the server phrases the same fact differently — and it does: the message for a create that
        the ledger already satisfied is "Ticket TCK-1025 already existed for this request; nothing
        was created", which contains neither word. The flag then reads false and the response
        claims a fresh ticket was created while the sentence next to it says otherwise.

        Both keys are checked because either is the same fact to a caller: a write answered from
        the ledger and a write confirmed by reading the API back are both "the backend did not
        create a second one".
        """
        for source in (result.data, result.structured or {}):
            if source.get("replayed") or source.get("recovered"):
                return True
        return False

    async def call(
        self, name: str, arguments: dict[str, Any], *, timeout_s: float | None = None
    ) -> ToolOutcome:
        """Call a tool and return its result, failures included as text.

        What the loop needs: a model must be shown what went wrong, so an error is a sentence in
        the transcript rather than an exception that aborts the turn — and the same result carries
        ``is_error``, so the loop can tell the *caller* that the call failed without reading that
        sentence for a marker it does not own.

        ``timeout_s`` is the slice the *turn* has left, and it wins over the ceiling this adapter
        was built with when it is smaller. The construction-time ceiling bounds one call in
        isolation; the turn's budget bounds the whole turn, and a call that could outlast it would
        make the turn's deadline meaningless — the last call would run past it and the reason
        reported would be about the call rather than about the turn.

        Checked with ``is None`` rather than truthiness, because ``0.0`` is a legitimate slice for
        a turn with nothing left and must not be silently replaced by the larger default.
        """
        effective = self._timeout_s if timeout_s is None else min(timeout_s, self._timeout_s)
        result = await self._client.call(name, arguments, timeout_s=effective)
        return ToolOutcome(
            text=result.as_model_text(),
            # The one-line sentence, for the response rather than for the model. `as_model_text`
            # carries the returned data as well, because a model given only the sentence invents
            # what it was not shown — and a page of tickets is not something to put in a tool row a
            # person is scanning. One call, two readers, each taken the half it needs.
            summary=result.message,
            is_error=result.is_error,
            replayed=self._replayed(result),
        )

    async def call_checked(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        """Call a tool and raise if it reported a failure.

        What the writer needs, and the split exists because one result has two readers with
        opposite requirements. The loop's ``call`` above renders a failure as the text
        ``ERROR: ...``, and the write path used to read that same string — so a refused write was
        recorded as ``executed``, since nothing in the value said otherwise. Raising is what makes
        the two readers unable to disagree: only one of them is given a value to compare.

        ``ToolCallFailedError`` rather than a bare ``RuntimeError`` so the writer's log and the
        approval row distinguish "the backend refused this" from "the transport broke", which are
        different things for whoever reads the row afterwards.
        """
        result = await self._client.call(name, arguments, timeout_s=self._timeout_s)
        if result.is_error:
            raise ToolCallFailedError(result.message)
        return ToolOutcome(
            text=result.as_model_text(),
            summary=result.message,
            is_error=False,
            replayed=self._replayed(result),
        )


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
    registry: ProviderRegistry,
    runtime: Runtime,
    sessions: sessionmaker[Session],
    settings: Settings,
    runs: InflightRegistry,
    decide: DecisionApplier,
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
            # Resolved per request, not captured at construction. This is what makes a provider
            # chosen on the page apply to the next turn: the handler factory reads the registry
            # when it is built, and the registry is swapped in place by the selection route. Read
            # once here rather than inside the loop, so every model turn of *this* invocation uses
            # the same provider and a mid-turn switch cannot produce a transcript written by two
            # different vendors.
            provider=registry.provider,
            tools=runtime.require_tools(),
            executor=runtime.require_executor(),
            limits=limits,
            trace_dir=settings.trace_dir,
            # The same registry the cancel route reads, so a run this starts is one the endpoint
            # can find. Two registries would mean a cancel that always reports nothing running.
            runs=runs,
            # The turn budget comes from the setting that names it. Until now it did not: this
            # call omitted it, so `AGENT_REQUEST_DEADLINE_S` was a documented knob that changed
            # nothing while every turn used the 90-second default. Found by the eval that needed
            # to drive a deadline, and worth stating plainly because a setting that silently does
            # nothing is worse than an absent one — an operator tuning it would conclude the
            # timeout logic was broken.
            turn_budget_s=settings.agent_request_deadline_s,
            # How long a cross-session fact stays current, passed rather than defaulted here for
            # the same reason: the setting exists, so the service is the thing that decides.
            fact_ttl_days=settings.agent_memory_fact_ttl_days,
            # How a decision arriving as a message is applied. The same callable the decision
            # route uses, handed the turn's own session, so the decision commits with the turn
            # rather than in a second transaction against the same SQLite file.
            decide=decide,
        )
        with sessions() as session:
            response = await factory(request, session)
            session.commit()
            return response

    return handle


def build_decision_callable(*, runtime: Runtime, settings: Settings) -> DecisionApplier:
    """The gate, wrapped so a decision can be applied to a session the *caller* owns.

    One implementation for two entry points, and the shape is the point: the decision route passes
    a session it opens per request, and the invocation handler passes the turn's own session so an
    approval that arrived as a message runs inside the transaction that carried it. Two
    implementations would be two sets of guarantees, and the second one would be the untested one.

    **The transition error becomes the route's conflict error.** The repository refuses a decision
    on a row that is already decided, expired, or not this user's, and raises
    ``ApprovalTransitionError``. `ApprovalGate` translates that into ``DecisionConflictError``,
    which the route turns into a 409 — without the translation the same refusal would surface as a
    500, telling a caller the service was broken when in fact their request was simply too late.
    Nothing is swallowed; the cause is chained.

    **The trace is written here, once per decision, rather than by either caller.** An approval is a
    state change that outlives the request that triggered it — a proposal may be answered long
    after it was made — so "what happened to this approval" has to be answerable from the
    approval's own record, not only from whichever conversation happened to be in flight.
    """

    async def decide(
        session: Session, approval_id: str, user_id: str, approved: bool
    ) -> ApprovalDecisionResult:
        approvals = ApprovalRepository(session)
        # Read for the trace's session id and for the span's name. ``None`` is legitimate — an id
        # belonging to someone else is reported exactly like one that does not exist — and the gate
        # below is what turns that into the refusal the caller sees.
        row = approvals.get(approval_id, user_id=user_id)
        tracer = Tracer(
            trace_dir=settings.trace_dir,
            session_id=row.session_id if row is not None else "",
            user_id=user_id,
        )
        action = row.action if row is not None else approval_id

        with tracer.span(EXECUTION, action, approved=approved) as span:
            gate = ApprovalGate.for_session(
                session,
                executor=WriteExecutor(
                    executor=runtime.require_executor(),
                    approvals=approvals,
                ),
            )
            try:
                result = await gate.decide(approval_id, user_id, approved)
            except DecisionConflictError as exc:
                span.attributes["outcome"] = "refused"
                # Written even for a refusal: "why did my approval do nothing" is a question about
                # state, and this row is the answer to it.
                TraceRepository(session).record(
                    tracer.finish(status="error", error=str(exc)),
                    user_id=user_id,
                )
                raise
            span.attributes["outcome"] = result.outcome

        TraceRepository(session).record(
            tracer.finish(
                status="error" if result.outcome == "failed" else "ok",
                error=result.detail if result.outcome == "failed" else None,
                # One tool call when a write ran, none otherwise: a declined decision executes
                # nothing, and the count is how that shows up in a trace listing.
                tool_calls=1 if result.outcome == "executed" else 0,
            ),
            user_id=user_id,
        )
        return result

    return decide


def build_decision_handler(
    *,
    decide: DecisionApplier,
    sessions: sessionmaker[Session],
) -> DecisionHandler:
    """Adapt the shared decision applier into the one-argument handler the route expects.

    The mismatch this resolves is the session: the route's handler takes an approval id, a user and
    a verdict, while the applier also needs a session to work in. This opens one per decision and
    commits it, which is what makes approving atomic — the decision row and the write it
    authorises are committed together, or not at all.
    """

    async def handle(approval_id: str, user_id: str, approved: bool) -> ApprovalDecisionResult:
        with sessions() as session:
            try:
                result = await decide(session, approval_id, user_id, approved)
            except DecisionConflictError:
                # Committed before the refusal propagates, so the trace explaining it survives.
                # Nothing else in this transaction needs rolling back — a refused decision changes
                # no row.
                session.commit()
                raise
            session.commit()
            return result

    return handle


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


def build_cancel_handler(*, runs: InflightRegistry) -> CancelHandler:
    """Turn a cancel request into a token being set on a running turn.

    The whole endpoint, and it is short because the design put the work where it belongs: the
    registry owns the scoping rule and the token owns the stopping condition. This only has to
    report what happened in words a person clicked for.

    ``None`` for "nothing was running" rather than an empty result, so the route can answer 404.
    That distinction is the point of the route's separate 501: a caller must be able to tell a
    finished turn from an unwired registry.
    """

    async def cancel(session_id: str, user_id: str) -> CancelResult | None:
        run = runs.cancel_session(session_id, user_id=user_id)
        if run is None:
            return None
        return CancelResult(
            session_id=session_id,
            state="stopping",
            detail=(
                "The turn has been asked to stop at its next step. A step already running — "
                "including a write — is allowed to finish."
            ),
        )

    return cancel


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
    # Resolved through a registry rather than built once and captured, so the provider can change
    # while the process serves. Its initial value is still the setting, so a deployment with no
    # stored preference behaves exactly as it did when this was a single `build_provider` call.
    registry = ProviderRegistry(resolved)
    control = ProviderControl(registry=registry, sessions=sessions)
    # A selection stored by an earlier run, if any. Applied before the handlers are built so the
    # very first request already answers with the chosen model — and a stored provider that can no
    # longer be built is a warning inside `restore`, never a failure to start.
    if control.restore():
        logger.info("using the stored model provider: %s", registry.current.provider)
    runtime = Runtime()
    # One registry for the process, created here rather than inside a handler, because the
    # invocation handler writes to it and the cancel handler reads from it — and a per-request
    # instance would mean the reader never saw what the writer wrote.
    runs = InflightRegistry()
    # Built once and given to both entry points — the decision route and the invocation handler — so
    # a button and a sentence cannot diverge in what they allow, record or trace. See
    # `build_decision_callable`.
    decide = build_decision_callable(runtime=runtime, settings=resolved)

    app = create_app(
        resolved,
        invocation_handler=build_invocation_handler(
            registry=registry,
            runtime=runtime,
            sessions=sessions,
            settings=resolved,
            runs=runs,
            decide=decide,
        ),
        decision_handler=build_decision_handler(decide=decide, sessions=sessions),
        pending_approvals=build_pending_listing(sessions=sessions),
        cancel_handler=build_cancel_handler(runs=runs),
        # The page's provider picker, reading and writing the same registry the loop uses, so a
        # chip that names a model and a turn that uses one cannot disagree.
        describe_providers=control.describe,
        choose_provider=control.choose,
        lifespan=build_lifespan(resolved, runtime),
    )
    app.state.engine = engine
    app.state.runtime = runtime
    app.state.runs = runs
    return app
