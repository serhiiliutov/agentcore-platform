"""The routes the page needs that the contract does not already provide.

Sending a message and answering a proposal are both already in the Runtime contract, so the page
calls them directly and this module has almost nothing to add. Only three things were missing, and
each had a reason.

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

**``GET /api/info`` says which model is answering.** Also for a person rather than for a machine:
the default provider is deterministic, so its replies are canned by design, and without this the
page gives a reader no way to know that the wording is a rule rather than a model — or that a real
model is one environment variable away. It reports the provider, the model if one is named, and
whether a credential is present; it reports no credential itself.

The handler is injected exactly as the invocation and decision handlers are, so this module can be
tested without a database and the app still starts without one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

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

#: Providers that answer from rules recorded in the repository. Not "not real" — they are what
#: makes the evals reproducible and free — but they do not reason, and the page should not imply
#: they do.
DETERMINISTIC_PROVIDERS = frozenset({"scripted", "replay"})

_DETERMINISTIC_NOTE = (
    "No language model is connected. AGENT_MODEL_PROVIDER=scripted answers from fixed rules in "
    "this repository, which is what makes the eval suite deterministic and free to run on every "
    "commit. To have a model choose the tools and answer, set AGENT_MODEL_PROVIDER to anthropic, "
    "openai, openai_compatible or bedrock with the matching credential set, then recreate the "
    "agent container. See README section c, and the note at the top of HOW-TO-TEST.md."
)

_LIVE_NOTE = (
    "A model chooses the tools. Which one is set by AGENT_MODEL_PROVIDER and AGENT_MODEL_ID, read "
    "once when the process starts."
)


class ListingNotConfiguredError(RuntimeError):
    """Raised by the default listing because nothing is wired to the database."""


async def _unconfigured_listing(_user_id: str) -> Sequence[PendingApproval]:
    raise ListingNotConfiguredError(
        "no approval listing is installed; it is supplied at application construction time"
    )


def create_ui_router(listing: PendingApprovalListing | None = None) -> APIRouter:
    """Build the page and listing routes.

    Both are on one router because both exist for the same reader, and splitting them would mean
    two things to include in `create_app` for one feature.
    """
    resolved = listing or _unconfigured_listing
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

    @router.get("/api/info", response_model=ServiceInfo)
    async def info(request: Request) -> ServiceInfo:
        """Report which provider is in force. Read-only, and it never fails.

        Read from ``app.state.settings``, which `create_app` sets, so there is no extra wiring and
        an app built without settings still answers rather than 500s — the page must render whether
        or not this works, and the chip simply stays hidden if it does not.

        Deliberately not part of the Runtime contract: it exists for a person looking at the page,
        and it says nothing about the conversation, the user or any credential beyond *whether* one
        is present for a provider that needs it.
        """
        settings: object = getattr(request.app.state, "settings", None)
        provider = str(getattr(settings, "agent_model_provider", "") or "unknown")
        model_id = str(getattr(settings, "agent_model_id", "") or "")
        configured: list[str] = []
        if settings is not None:
            described = getattr(settings, "configured_providers", None)
            if callable(described):
                configured = [str(name) for name in described()]

        deterministic = provider in DETERMINISTIC_PROVIDERS
        return ServiceInfo(
            provider=provider,
            model_id=model_id or None,
            deterministic=deterministic,
            configured=configured,
            detail=_DETERMINISTIC_NOTE if deterministic else _LIVE_NOTE,
        )

    return router
