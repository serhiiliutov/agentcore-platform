"""Which provider answers, and how that can change while the service is running.

`config.Settings.agent_model_provider` is read once at startup, which is enough for a deployment
that picks a provider and keeps it. It is not enough for the question a reviewer actually asks —
"can I see this with a real model?" — where changing provider meant editing `.env`, recreating a
container and losing the conversation on screen. This module is the missing half: one small object
that holds the provider currently in force, swaps it on request, and reports what is available.

**It does not replace the setting; it starts from it.** `Settings` is still the initial value, and
a process with no database row answers exactly as it did before. What changes is that a choice made
at runtime can be *persisted* and found again after a restart, which is what the `provider_settings`
table was reserved for.

**A swap is validated before it is adopted.** The candidate provider is constructed first, so a
missing key or an unusable model fails at the moment somebody asked for it — with a message — rather
than on the next user turn. Selection is not a promise that the credential is valid, only that the
provider could be built; nothing here makes a model call.

**Swapping is atomic and needs no lock on the read path.** The reference is rebound in one
assignment, and readers take it once per turn, so a turn either uses the provider it started with
or the new one — never a half-built instance. The lock exists for the writers, so two simultaneous
selections cannot interleave their validate-then-build steps.

**The credential never leaves the process.** A selection is a name and a model id. The key is read
from `Settings` when the provider is built, lives inside that instance, and appears in no response
this module produces — the same rule the rest of the service follows, asserted by the tests in
`tests/unit/test_providers.py`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import cast

from agent_service.config import ProviderName, Settings

from .base import ModelProvider, ProviderNotConfiguredError, build_provider

logger = logging.getLogger(__name__)

#: Providers that answer from rules in this repository rather than from a model. Not "not real":
#: they are what makes the eval suite reproducible and free to run on every commit, and they are
#: the default so a bare checkout works with no credential. Defined here rather than in the API
#: layer because it is a property of a provider, and two definitions would drift.
DETERMINISTIC_PROVIDERS: frozenset[str] = frozenset({"scripted", "replay"})

#: How many model ids a listing returns. The OpenAI catalogue runs to dozens of entries, most of
#: them irrelevant to a chat turn, and a `<datalist>` with eighty options is worse than one with
#: the ten an operator might plausibly want.
MODEL_LIST_LIMIT = 40


def is_deterministic(provider: str) -> bool:
    """Whether this provider answers from recorded rules rather than from a model."""
    return provider in DETERMINISTIC_PROVIDERS


@dataclass(frozen=True)
class ProviderSelection:
    """A provider and the model it should use.

    ``model_id`` empty means "whatever that provider defaults to", which is deliberately the
    same convention as `Settings.agent_model_id` — so a selection and a setting can be compared,
    stored and logged as one thing.
    """

    provider: ProviderName
    model_id: str = ""

    @property
    def deterministic(self) -> bool:
        return is_deterministic(self.provider)


def default_model_for(provider: ProviderName) -> str:
    """The model this provider falls back to when none is named.

    Read from each provider module rather than repeated here, because a second copy of a model id
    is a second thing to forget to update: `openai_provider.DEFAULT_MODEL` and its Anthropic and
    Bedrock counterparts are what the providers themselves use when `agent_model_id` is empty, and
    the page should offer the same string the service would actually send.
    """
    # Imported as modules rather than as `DEFAULT_MODEL`: three aliases of one constant name is what
    # N811 exists to catch, and it is right — the alias hides that all three are the same concept.
    if provider == "anthropic":
        from . import anthropic_provider

        return anthropic_provider.DEFAULT_MODEL
    if provider in ("openai", "openai_compatible"):
        from . import openai_provider

        return openai_provider.DEFAULT_MODEL
    if provider == "bedrock":
        from . import bedrock_provider

        return bedrock_provider.DEFAULT_MODEL
    # scripted and replay answer from this repository; neither has a model id to offer, and
    # `replay` uses the field for a file path, which is not a model at all.
    return ""


class ProviderRegistry:
    """The provider in force, and the operations that change it.

    Constructed once per process by the composition root and handed to the invocation handler and
    the UI routes, so both read the same answer: a page that showed one model while the agent used
    another would be worse than showing nothing.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self._selection = ProviderSelection(
            provider=settings.agent_model_provider,
            model_id=settings.agent_model_id,
        )
        # Built eagerly, so a misconfigured startup is still a startup failure with a clear
        # message rather than an error inside the first user turn.
        self._provider: ModelProvider = self._build(self._selection)

    # --- reading ----------------------------------------------------------

    @property
    def current(self) -> ProviderSelection:
        """The selection in force. A plain attribute read: no lock, no await."""
        return self._selection

    @property
    def provider(self) -> ModelProvider:
        """The provider instance to use for one turn.

        Read once per turn by the caller and used for the whole turn, so a swap between two model
        turns of the same invocation cannot produce a transcript written by two different vendors.
        """
        return self._provider

    @property
    def settings(self) -> Settings:
        return self._settings

    def available(self) -> tuple[ProviderName, ...]:
        """Providers this process could build right now — `Settings`'s own answer."""
        return self._settings.configured_providers()

    def usable(self, provider: str) -> bool:
        return provider in self.available()

    def is_deterministic_now(self) -> bool:
        return self._selection.deterministic

    # --- changing ---------------------------------------------------------

    async def select(self, provider: str, model_id: str = "") -> ProviderSelection:
        """Adopt a provider and model, or raise with a reason.

        ``provider`` is a plain string, not `ProviderName`, and that is deliberate: the caller is
        usually a request body, whose value is a string until something validates it, and this method
        *is* the validation. Typing the parameter as the Literal would push the claim onto the HTTP
        layer, which cannot honestly make it — the best it could do is a cast, which is the same
        unchecked assertion written somewhere less true.

        The candidate is built *before* anything is adopted, so a refusal leaves the service
        exactly as it was — a selection that failed must not take the working provider down with
        it. `ProviderNotConfiguredError` carries the reason to the caller, which puts it in the
        response the page shows.
        """
        if not self.usable(provider):
            raise ProviderNotConfiguredError(
                f"provider {provider!r} is not usable in this process; available: "
                f"{', '.join(self.available())}"
            )

        # Narrowed here because the check immediately above is what makes it true: every name in
        # `available()` came from the `ProviderName` Literal.
        selection = ProviderSelection(
            provider=cast(ProviderName, provider), model_id=model_id.strip()
        )
        candidate = self._build(selection)

        async with self._lock:
            self._selection = selection
            self._provider = candidate

        logger.info(
            "model provider selected",
            extra={"provider": selection.provider, "model_id": selection.model_id or "(default)"},
        )
        return selection

    def adopt(self, selection: ProviderSelection) -> bool:
        """Adopt a persisted selection at startup. Returns whether it was adopted.

        Deliberately not an exception on failure. This runs while the service is starting, from a
        row somebody wrote in an earlier session, and a key that has since been removed must not
        stop the service from serving — it should fall back to what the environment says and say so
        in the log. That is the difference between a stored preference and a startup requirement.
        """
        if not self.usable(selection.provider):
            logger.warning(
                "stored model provider is no longer usable; keeping the configured one",
                extra={"stored": selection.provider, "keeping": self._selection.provider},
            )
            return False
        try:
            candidate = self._build(selection)
        except ProviderNotConfiguredError as exc:
            logger.warning(
                "stored model provider could not be built; keeping the configured one",
                extra={"stored": selection.provider, "reason": str(exc)},
            )
            return False

        self._selection = selection
        self._provider = candidate
        logger.info(
            "adopted the stored model provider",
            extra={"provider": selection.provider, "model_id": selection.model_id or "(default)"},
        )
        return True

    # --- what the page offers --------------------------------------------

    async def list_models(self, provider: ProviderName) -> tuple[str, ...]:
        """Model ids to offer for a provider: the live catalogue where there is one.

        Only the OpenAI-shaped providers are asked, because only they publish a listing endpoint
        that a key can be pointed at. The others fall back to the id the provider itself would
        use, which is a suggestion rather than a limit — the model field stays free text, since
        the point of the setting is that any id the operator knows can be typed into it.

        A failure is not an error here. This feeds a `<datalist>`; if the catalogue cannot be read
        the right answer is the default model and a working page, not a broken settings panel.
        """
        default = default_model_for(provider)
        if provider not in ("openai", "openai_compatible"):
            return (default,) if default else ()

        try:
            listed = await self._openai_model_ids()
        except Exception as exc:
            logger.info(
                "could not list models; offering the provider default",
                extra={"provider": provider, "reason": str(exc)},
            )
            return (default,) if default else ()

        if not listed:
            return (default,) if default else ()
        # The provider's own default first, then the rest: it is the id a blank field would use,
        # so it should be the one a reader sees at the top of the list.
        ordered = ([default] if default and default in listed else []) + sorted(listed)
        return tuple(dict.fromkeys(ordered))[:MODEL_LIST_LIMIT]

    async def _openai_model_ids(self) -> list[str]:
        """Ask an OpenAI-shaped endpoint which models it serves.

        Constructed here rather than reusing the provider instance, because the provider holds a
        chat client and no listing method — and reusing it would mean the registry reaches inside
        a provider, which is the coupling the protocol exists to prevent.
        """
        key = self._settings.openai_api_key
        base_url = self._settings.openai_base_url.strip()
        if key is None and not base_url:
            return []

        from openai import AsyncOpenAI

        # The same explicit default the provider uses, for the same reason: `None` makes the SDK
        # read its own `OPENAI_BASE_URL`, which compose leaves empty rather than unset. See
        # `openai_provider.DEFAULT_BASE_URL`.
        from .openai_provider import DEFAULT_BASE_URL

        client = AsyncOpenAI(
            api_key=key.get_secret_value() if key is not None else "not-needed",
            base_url=base_url or DEFAULT_BASE_URL,
        )
        page = await client.models.list()
        entries = getattr(page, "data", None) or ()
        return [str(getattr(entry, "id", "")) for entry in entries if getattr(entry, "id", "")]

    # --- internals --------------------------------------------------------

    def _build(self, selection: ProviderSelection) -> ModelProvider:
        """Construct a provider from a selection, over the process's own settings.

        `model_copy` rather than a fresh `Settings`: the point is to keep every other value — the
        keys, the base URL, the AWS region — exactly as the environment set it, and change only
        which of them is being used. Re-reading the environment here would let a long-running
        process drift from the settings it started with.
        """
        scoped = self._settings.model_copy(
            update={
                "agent_model_provider": selection.provider,
                "agent_model_id": selection.model_id,
            }
        )
        return build_provider(scoped)
