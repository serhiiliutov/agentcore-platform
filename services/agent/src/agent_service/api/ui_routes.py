"""The routes the page needs that the contract does not already provide.

Sending a message and answering a proposal are both already in the Runtime contract, so the page
calls them directly and this module has almost nothing to add. What is here is the handful of things
the contract has no room for, and each of them has a reason.

**``GET /`` serves the page.** Inline rather than mounted from a directory: the HTML is a Python
constant, so there is no static directory that can fail to be copied into an image, and no path
that has to exist before the app can start.

**``GET /api/approvals`` lists outstanding proposals for a user.** This route exists because of a
specific requirement rather than for convenience: *a pending approval must survive a restart of
the service*. That guarantee is worthless from the front end if the only way to see a proposal is
to have just created it in the same page — restart the service, reload the page, and an
approvable write would be invisible. So the listing is fetched on load, from the database, which
is where the approval was made durable.

The listing reuses `PendingApproval`, the same type the invocation response carries, rather than
defining a second shape for a proposal. Two shapes for one thing is how a field gets added to one
of them and not the other, and the page would then render a proposal differently depending on
which endpoint produced it.

**``GET /api/sessions`` and ``GET /api/sessions/{id}/messages`` reopen an earlier conversation.**
`CONTRACT.md` is one request in and one reply out, so nothing in it can be asked "which
conversations do I have?" — which means a front end can only ever continue the conversation it is
already in, and every reload strands the previous one. It takes two endpoints rather than one
because they answer different questions at different sizes: a *menu* of a user's conversations,
bounded at twenty, and *one* conversation in full. Drawing the picker from the second would read a
whole conversation to render every row of a list.

**``GET /api/info`` says which model is answering.** Also for a person rather than for a machine:
the default provider is deterministic, so its replies are canned by design, and without this the
page gives a reader no way to know that the wording is a rule rather than a model — or that a real
model is one environment variable away. It reports the provider, the model if one is named, and
whether a credential is present; it reports no credential itself.

**``GET /api/providers`` and ``PUT /api/providers/current`` choose the model.** These exist because
"the model provider should be swappable through config" is satisfied by an environment variable and
*felt* by a person as "I have to edit a file and restart". The pair reports what this process could
build and adopts a choice, and the wiring layer persists it — so a reviewer can point the agent at a
real model from the page, see the difference in the next reply, and still find that choice selected
after a restart. `PUT` rather than `POST` because the resource is the current selection and replacing
it is exactly what happens: there is one, and it is being set, not accumulated.

Neither route accepts a credential. A key reaches the process through the environment only, so the
page can choose *whose* key is used and never carries one — which is what would otherwise be the
obvious way to build this and the wrong one.

The handler is injected exactly as the invocation and decision handlers are, so this module can be
tested without a database and the app still starts without one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

from agent_service.providers.registry import is_deterministic
from agent_service.ui import INDEX_HTML

from .models import PendingApproval


class ApprovalList(BaseModel):
    """The response shape for the listing.

    An object with an ``approvals`` key rather than a bare array. A top-level array cannot grow a
    field later — a count, a cursor, a "these were truncated" flag — without breaking every client
    that already parses it, and a listing is exactly the kind of endpoint that grows one.
    """

    model_config = ConfigDict(extra="ignore")

    approvals: list[PendingApproval] = []


class SessionSummary(BaseModel):
    """One of a user's conversations, as a front end lists it.

    Deliberately not the session id alone. A picker that showed a column of
    `chainlit-3f9a2c81b4e7` would be unusable, so each row carries what a person actually
    recognises: the opening of what they typed, when it was, and how long it got.
    """

    model_config = ConfigDict(extra="ignore")

    session_id: str
    #: Stored turns in it — the user's and the assistant's together.
    messages: int = 0
    #: When it was last written to. ``None`` only if a session somehow has messages with no
    #: timestamp, which the schema does not allow.
    last_at: datetime | None = None
    #: The opening of the first thing the user said, truncated. Empty for a session whose first
    #: message was not theirs, which cannot happen through this service's own loop.
    preview: str = ""


class SessionList(BaseModel):
    """The listing, wrapped for the reason `ApprovalList` is: an array cannot grow a field."""

    model_config = ConfigDict(extra="ignore")

    sessions: list[SessionSummary] = []


class TranscriptMessage(BaseModel):
    """One stored message from a conversation, for a front end replaying it.

    Closer to the database row than the provider's message shape, and deliberately so: the two
    readers want different things. A provider needs only what it must be shown — content, role, the
    call it belongs to — while somebody resuming a conversation needs to see what happened, which
    includes the order and the time.

    In practice the rows are user and assistant turns. This service's loop deliberately stores the
    conversation and not the tool calls in between (`agent/handler.py::_turn_messages`), so
    ``tool_name`` is normally absent — it is carried because the column exists and a row that had one
    would otherwise lose it, not because a replay should expect tool steps.
    """

    model_config = ConfigDict(extra="ignore")

    sequence: int
    #: ``user``, ``assistant``, ``tool`` or ``system``. Passed through as a string rather than
    #: narrowed to a union, because the only consumer renders it — and a role this service stopped
    #: writing should not make an older transcript unreadable.
    role: str
    content: str
    #: Set on a tool call or its result, so a replay can label the row instead of printing JSON.
    tool_name: str | None = None
    created_at: datetime | None = None


class Transcript(BaseModel):
    """A whole conversation, oldest first.

    Wrapped for the reason the other responses are — an array cannot grow a field — and for one
    specific to this endpoint: a replay has to say *which* conversation it is showing, and that
    fact has nowhere to live in a bare list.
    """

    model_config = ConfigDict(extra="ignore")

    session_id: str
    messages: list[TranscriptMessage] = []


class ServiceInfo(BaseModel):
    """Which model is answering, for the page's own header.

    Added because the default provider is deterministic. A reviewer who does not know that reads
    canned sentences — "Done. I used 1 tool call(s) to answer." — and has no way to tell them from
    a live model's, so the page says it outright, and says how to change it. That is the same
    answer the README gives, shown where the question is actually asked.
    """

    model_config = ConfigDict(extra="ignore")

    provider: str
    model_id: str | None = None
    #: True when the provider answers from rules in this repository rather than from a model.
    deterministic: bool
    #: What `Settings.configured_providers()` says this process believes it can use: the
    #: credential-free providers, plus any whose credential or endpoint it can see. A *blank* key
    #: is absent rather than a credential — that distinction was a defect until it was fixed, and
    #: it is why this list is worth showing at all. `bedrock` appears whenever a region is set,
    #: because the AWS SDK may find credentials in a chain this process cannot inspect (a profile,
    #: an instance role), so the list is a belief rather than a promise that a call will
    #: authenticate.
    configured: list[str] = []
    detail: str


#: Lists a user's outstanding proposals. Supplied by the wiring layer, like the other handlers.
PendingApprovalListing = Callable[[str], Awaitable[Sequence[PendingApproval]]]
#: Lists a user's conversations, newest first. Also supplied by the wiring layer.
SessionListing = Callable[[str], Awaitable[Sequence[SessionSummary]]]
#: Reads one conversation's messages. Takes the session id *and* the user, because a transcript is
#: the most sensitive read here and the owner check cannot be left to whoever supplied the id.
TranscriptReader = Callable[[str, str], Awaitable[Sequence[TranscriptMessage]]]


class ProviderStatus(BaseModel):
    """What the provider picker needs, and what ``/api/info`` summarises.

    One shape for both, because they are one fact: which model is answering. `/api/info` reports it
    for the header chip, the picker reports it with the alternatives beside it, and a second model
    for the same state is how a page ends up showing a provider the agent is not using.
    """

    model_config = ConfigDict(extra="ignore")

    provider: str
    model_id: str | None = None
    deterministic: bool
    #: Providers this process could build right now. Names only — never key material, and never
    #: whether a key is *valid*, which only a model call can say.
    available: list[str] = []
    #: Model ids to offer for the current provider. Suggestions rather than a whitelist: the model
    #: field stays free text, because an operator who knows an id the catalogue does not list must
    #: still be able to type it.
    models: list[str] = []
    detail: str


class ProviderChoice(BaseModel):
    """The body of a selection request.

    ``model_id`` defaults to empty, which means "this provider's own default" — the same convention
    `Settings.agent_model_id` uses, so a client can switch provider without knowing model names.
    """

    model_config = ConfigDict(extra="ignore")

    provider: str
    model_id: str = ""


#: Reads the selection in force, with the alternatives. Supplied by the wiring layer.
DescribeProviders = Callable[[], Awaitable[ProviderStatus]]
#: Adopts a selection, or raises `ProviderSelectionError` with the reason. Also supplied.
ChooseProvider = Callable[[str, str], Awaitable[ProviderStatus]]


class ProviderSelectionError(RuntimeError):
    """A selection was refused, and the message says why.

    Raised by the wiring layer for a provider this process cannot build — a missing key, an
    unavailable endpoint — and turned into a 400 rather than a 500 here. The distinction matters to
    whoever is looking at the page: a refused choice is something they can fix, and a 500 would tell
    them the service is broken instead.
    """


class ProviderControlNotWiredError(RuntimeError):
    """Raised by the default handlers, because nothing is wired to a provider registry."""


async def _unconfigured_describe() -> ProviderStatus:
    raise ProviderControlNotWiredError(
        "no provider control is installed; it is supplied at application construction time"
    )


async def _unconfigured_choose(_provider: str, _model_id: str) -> ProviderStatus:
    raise ProviderControlNotWiredError(
        "no provider control is installed; it is supplied at application construction time"
    )


_DETERMINISTIC_NOTE = (
    "No language model is connected: this provider answers from fixed rules in the repository, "
    "which is what makes the eval suite deterministic and free to run on every commit. Pick a "
    "provider in the model control above, or set AGENT_MODEL_PROVIDER (anthropic, openai, "
    "openai_compatible, bedrock) with the matching credential in the environment. The key always "
    "comes from the environment; the page chooses which one is used and never carries one."
)

_LIVE_NOTE = (
    "A model chooses the tools. The provider and model id are chosen in the model control above "
    "and persist across restarts; AGENT_MODEL_PROVIDER and AGENT_MODEL_ID set the initial value."
)


class ListingNotConfiguredError(RuntimeError):
    """Raised by a default listing because nothing is wired to the database.

    Shared by both listings — approvals and sessions — because the condition is the same one and
    the caller's response to it is identical: a 501 saying this endpoint exists but the wiring
    layer has not supplied its reader. Each raises it with its own sentence, so the message still
    names which listing is missing.
    """


async def _unconfigured_listing(_user_id: str) -> Sequence[PendingApproval]:
    raise ListingNotConfiguredError(
        "no approval listing is installed; it is supplied at application construction time"
    )


async def _unconfigured_sessions(_user_id: str) -> Sequence[SessionSummary]:
    raise ListingNotConfiguredError(
        "no session listing is installed; it is supplied at application construction time"
    )


async def _unconfigured_transcript(_session_id: str, _user_id: str) -> Sequence[TranscriptMessage]:
    raise ListingNotConfiguredError(
        "no transcript reader is installed; it is supplied at application construction time"
    )


def create_ui_router(
    listing: PendingApprovalListing | None = None,
    *,
    describe_providers: DescribeProviders | None = None,
    choose_provider: ChooseProvider | None = None,
    list_sessions: SessionListing | None = None,
    read_transcript: TranscriptReader | None = None,
) -> APIRouter:
    """Build the page, listing, conversation and model-selection routes.

    All of them are on one router because all of them exist for the same reader, and splitting them
    would mean five things to include in `create_app` for one feature. The provider and conversation
    handlers are optional and keyword-only, so every existing caller — including the contract tests
    that build an app with nothing installed — keeps working unchanged.
    """
    resolved = listing or _unconfigured_listing
    describe = describe_providers or _unconfigured_describe
    choose = choose_provider or _unconfigured_choose
    sessions = list_sessions or _unconfigured_sessions
    transcript = read_transcript or _unconfigured_transcript
    router = APIRouter(tags=["ui"])

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        """The page. Served from a constant — see the module docstring."""
        return HTMLResponse(INDEX_HTML)

    @router.get("/api/approvals", response_model=ApprovalList)
    async def approvals(
        user_id: str = Query(min_length=1, max_length=200),
    ) -> object:
        """Outstanding proposals for one user, newest first.

        ``user_id`` is required and validated the same way every other user-scoped read validates
        it. Without it there is no safe answer — listing every pending proposal in the system would
        show one person another's proposed writes, which is the disclosure the whole approval
        design exists to avoid.

        An unwired listing answers 501 rather than 500, so "not set up" stays distinguishable from
        "broken" here as it does on the other endpoints.
        """
        try:
            found = await resolved(user_id)
            return ApprovalList(approvals=list(found))
        except ListingNotConfiguredError as exc:
            return JSONResponse(
                status_code=501,
                content={"error": {"kind": "NotImplemented", "detail": str(exc)}},
            )

    @router.get("/api/sessions", response_model=SessionList)
    async def sessions_for_user(
        user_id: str = Query(min_length=1, max_length=200),
    ) -> object:
        """This user's conversations, newest first — what a front end resumes from.

        ``user_id`` is required for the same reason `/api/approvals` requires it, and the exposure
        is the same shape one level up: without it this would hand a caller the opening line of every
        conversation in the system, which is a transcript leak with a friendly summary on top.

        Deliberately not paginated. Twenty rows is the whole menu for a chat sidebar, and the case
        beyond it wants a search rather than a longer list — a cursor here would complicate the only
        caller to serve a scroll nobody asked for.
        """
        try:
            found = await sessions(user_id)
            return SessionList(sessions=list(found))
        except ListingNotConfiguredError as exc:
            return JSONResponse(
                status_code=501,
                content={"error": {"kind": "NotImplemented", "detail": str(exc)}},
            )

    @router.get("/api/sessions/{session_id}/messages", response_model=Transcript)
    async def session_transcript(
        session_id: str = Path(min_length=1, max_length=200),
        user_id: str = Query(min_length=1, max_length=200),
    ) -> object:
        """One conversation's messages, oldest first — what a chat redraws to resume it.

        Both identifiers are required, and the user id is not merely a filter here. Without it this
        endpoint would serve any conversation to anyone who could guess an id, and a session id is
        not a secret — the page shows the current one, and it appears in logs. The owner check is
        the reader's, deliberately: it is the layer that can scope the query, and putting it in the
        route would leave a second call site able to forget it.

        A conversation belonging to somebody else answers with an empty transcript rather than a
        refusal. That matches `SessionRepository.ensure`, where an id that is not yours must be
        indistinguishable from one that does not exist — a "forbidden" would turn a guessed id into
        a way to confirm it exists.
        """
        try:
            found = await transcript(session_id, user_id)
        except ListingNotConfiguredError as exc:
            return JSONResponse(
                status_code=501,
                content={"error": {"kind": "NotImplemented", "detail": str(exc)}},
            )
        return Transcript(session_id=session_id, messages=list(found))

    @router.get("/api/info", response_model=ServiceInfo)
    async def info(request: Request) -> ServiceInfo:
        """Report which provider is in force. Read-only, and it never fails.

        Asked of the provider control when one is installed, and of ``app.state.settings`` when it
        is not. The order matters and used to be the other way round: reading the startup setting
        would keep reporting the provider the process *began* with after somebody changed it on the
        page, so the header chip would name a model that was no longer answering — the one thing
        this endpoint exists to get right.

        Deliberately not part of the Runtime contract: it exists for a person looking at the page,
        and it says nothing about the conversation, the user or any credential beyond *whether* one
        is present for a provider that needs it.
        """
        status = await _maybe_describe(describe)
        if status is not None:
            return ServiceInfo(
                provider=status.provider,
                model_id=status.model_id,
                deterministic=status.deterministic,
                configured=status.available,
                detail=status.detail,
            )

        # No control installed: answer from settings, so an app built without a registry still
        # renders a page and a chip rather than a 500. This is the path the contract tests take.
        settings: object = getattr(request.app.state, "settings", None)
        provider = str(getattr(settings, "agent_model_provider", "") or "unknown")
        model_id = str(getattr(settings, "agent_model_id", "") or "")
        configured: list[str] = []
        if settings is not None:
            described = getattr(settings, "configured_providers", None)
            if callable(described):
                configured = [str(name) for name in described()]

        deterministic = is_deterministic(provider)
        return ServiceInfo(
            provider=provider,
            model_id=model_id or None,
            deterministic=deterministic,
            configured=configured,
            detail=_DETERMINISTIC_NOTE if deterministic else _LIVE_NOTE,
        )

    @router.get("/api/providers", response_model=ProviderStatus)
    async def providers() -> object:
        """Which provider is in force, which others could be, and which models to suggest.

        The listing is what the page draws its picker from, so all three answers are needed at
        once: a picker that knew the alternatives but not the current one would guess, and one that
        knew both but offered no model ids would make the operator type an id from memory.

        A 501 when nothing is wired, matching the other endpoints: the client can tell "not
        configured" from "broken", and the build without a registry stays honest about what it has.
        """
        try:
            return await describe()
        except ProviderControlNotWiredError as exc:
            return JSONResponse(
                status_code=501,
                content={"error": {"kind": "NotImplemented", "detail": str(exc)}},
            )

    @router.put("/api/providers/current", response_model=ProviderStatus)
    async def select_provider(payload: ProviderChoice) -> object:
        """Adopt a provider — and optionally a model — for subsequent turns.

        A refusal is a 400 with the reason, never a 500: the reasons this can fail are all things
        the caller can act on (no key for that provider in this process, a provider name that is not
        one of ours, a stored row that no longer builds). The message is the wiring layer's own, so
        it names the provider and what is missing rather than saying "invalid request".

        The body carries no credential, and the response carries none either — see the module
        docstring. What this endpoint changes is *which* configured credential is used, not what any
        credential is.
        """
        try:
            return await choose(payload.provider, payload.model_id)
        except ProviderSelectionError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": {"kind": "ProviderNotConfigured", "detail": str(exc)}},
            )
        except ProviderControlNotWiredError as exc:
            return JSONResponse(
                status_code=501,
                content={"error": {"kind": "NotImplemented", "detail": str(exc)}},
            )

    return router


async def _maybe_describe(describe: DescribeProviders) -> ProviderStatus | None:
    """The current provider status, or ``None`` when no control is installed.

    A helper rather than a try/except inlined in the route because two routes need the same
    decision and only one of them should treat "not wired" as an error: `/api/providers` answers
    501, while `/api/info` must still answer. Putting the difference in the caller keeps the rule
    visible — an endpoint that exists to describe the service must not be the one that fails when
    part of it is missing.
    """
    try:
        return await describe()
    except ProviderControlNotWiredError:
        return None
