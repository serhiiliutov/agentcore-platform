"""The picker's two handlers: report the selection, and adopt a new one — durably.

This is the seam between `providers/registry.py`, which knows how to build and swap a provider, and
`api/client_routes.py`, which knows the HTTP shape but must not know a database exists. It is the same
seam `build_pending_listing` occupies for approvals, and it exists for the same reason: the route
module stays testable with a fake handler, and the wiring lives where the wiring can be read.

**A change is persisted before it is reported.** The registry swaps in memory and this writes the
row in the same request, so "the picker said OpenAI" and "the next restart picks OpenAI" cannot
disagree. Writing first would risk a row describing a provider that failed to build; reporting
first would risk the opposite.

**A failed selection leaves everything as it was.** The registry builds the candidate before
adopting it, and this only writes the row once that has succeeded — so a provider with no key is
refused with a message, the running provider keeps answering, and the stored row still describes
what is actually in force. That property is worth the ordering: it is what lets a client offer a
list of providers including ones that might not work, without a mis-click disabling the service.

**The listing is best-effort; the selection is not.** `describe` asks the provider for its model
catalogue and tolerates a failure, because that only fills a `<datalist>`. `choose` does not
tolerate anything: either the provider was built and stored, or the caller gets a reason.

**No key is read, written or returned here.** The row holds a provider name and a model id. The
credential stays in `Settings`, which the registry copies when it builds — so this module has no
path that could put a key in a response, a log line or the database.
"""

from __future__ import annotations

import logging
from typing import cast, get_args

from sqlalchemy.orm import Session, sessionmaker

from agent_service.config import ProviderName
from agent_service.db.repositories.provider_settings import ProviderSettingsRepository
from agent_service.providers.base import ProviderNotConfiguredError
from agent_service.providers.registry import ProviderRegistry, ProviderSelection, is_deterministic

from .client_routes import ProviderSelectionError, ProviderStatus

logger = logging.getLogger(__name__)

#: What a client shows beside the chip, in each case. Wording lives here rather than in the route
#: module because the wired service is described by the wiring — the route's own copy is only for
#: the unwired build, where there is no registry to ask.
_DETERMINISTIC_DETAIL = (
    "Answers from fixed rules in this repository — that is what makes the eval suite deterministic "
    "and free on every commit. Choose a provider to let a model pick the tools and word the reply."
)

_LIVE_DETAIL = (
    "A model chooses the tools and writes the reply. The choice is stored and survives a restart; "
    "the credential comes from the environment and is never sent to a client."
)

#: The provider names this service understands, from the Literal itself rather than from a second
#: list. `get_args` keeps the two in step: widening `ProviderName` widens what this accepts, and
#: `build_provider` fails loudly for a name it has no branch for, so a widened Literal cannot be
#: accepted here and then silently unsupported.
KNOWN_PROVIDERS: tuple[str, ...] = get_args(ProviderName)


class ProviderControl:
    """Reports and changes the model provider, with the change stored."""

    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        sessions: sessionmaker[Session],
    ) -> None:
        self._registry = registry
        self._sessions = sessions

    def restore(self) -> bool:
        """Adopt the stored selection, if there is one. Returns whether it was adopted.

        Called once while the application is being built. A stored provider that this process can no
        longer build — the key was removed, the endpoint is gone — is a warning and not a failure:
        a preference must not be able to stop the service from starting, and the log says which
        provider is being kept instead.
        """
        with self._sessions() as session:
            row = ProviderSettingsRepository(session).current()

        if row is None:
            return False

        # Checked before the cast rather than after, and checked here rather than left to the
        # registry's own refusal: a stored name that this build no longer knows is a row written by
        # a different version of the service, and it deserves a log line that says so instead of a
        # silent "not usable". The cast is then a formality — the membership test immediately above
        # is what makes it true, which is the only condition under which a cast is honest.
        stored = str(row.provider)
        if stored not in KNOWN_PROVIDERS:
            logger.warning(
                "stored model provider is not one this build knows; keeping the configured one",
                extra={"stored": stored, "known": KNOWN_PROVIDERS},
            )
            return False

        return self._registry.adopt(
            ProviderSelection(provider=cast(ProviderName, stored), model_id=row.model_id)
        )

    async def describe(self) -> ProviderStatus:
        """What is in force, what else could be, and which models to suggest."""
        current = self._registry.current
        models = await self._registry.list_models(current.provider)

        return ProviderStatus(
            provider=current.provider,
            model_id=current.model_id or None,
            deterministic=current.deterministic,
            available=list(self._registry.available()),
            models=list(models),
            detail=_DETERMINISTIC_DETAIL if current.deterministic else _LIVE_DETAIL,
        )

    async def choose(self, provider: str, model_id: str = "") -> ProviderStatus:
        """Adopt a provider and model, store the choice, and report the result.

        Three refusals, each with its own sentence, because they need different fixes: a name this
        service does not have, a provider it has but cannot build (no key, no endpoint), and — from
        the registry — the same construction failure seen from further in. All three become
        `ProviderSelectionError`, which the route turns into a 400.
        """
        if provider not in KNOWN_PROVIDERS:
            raise ProviderSelectionError(
                f"{provider!r} is not a provider this service knows; "
                f"it offers: {', '.join(KNOWN_PROVIDERS)}"
            )

        try:
            selection = await self._registry.select(provider, model_id)
        except ProviderNotConfiguredError as exc:
            raise ProviderSelectionError(str(exc)) from exc

        # Stored only after the swap succeeded, so the row never describes a provider that is not
        # the one answering. Committed here rather than by the caller: this is the whole transaction
        # the request exists to perform, and leaving it open would let the response be sent before
        # the choice was durable.
        with self._sessions() as session:
            ProviderSettingsRepository(session).store(
                provider=selection.provider,
                model_id=selection.model_id,
                note="selected in the chat",
            )
            session.commit()

        return await self.describe()

    def current_is_deterministic(self) -> bool:
        """Whether the provider in force answers from rules. For the log line at startup."""
        return is_deterministic(self._registry.current.provider)
