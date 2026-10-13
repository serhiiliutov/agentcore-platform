"""The provider-neutral types the agent loop speaks.

ADR-0002 fixes the shape: ``chat(messages, tools, *, budget) -> AssistantTurn``. Everything
below exists so that the loop never mentions a vendor.

Three translation problems live here rather than in each provider, because getting them wrong
is how a loop becomes vendor-shaped without anyone noticing:

**Tool calls are provider-neutral.** Anthropic returns content blocks, OpenAI returns
``tool_calls`` with a JSON *string* for arguments, Bedrock returns a structured object. All
three become `ToolCallRequest` with already-parsed arguments, so the loop never parses JSON it
did not expect to parse.

**Usage is recorded, not interpreted.** Token counts differ in meaning between vendors, so
they are carried for the trace and never used for control flow. A budget is a turn count, not
a token count — which keeps the loop's termination condition comparable across providers.

**A provider that cannot run says so before it is asked.** `build_provider` raises
`ProviderNotConfiguredError` for a provider whose key or base URL is missing, rather than
letting the first `chat` fail with an authentication error three turns into a conversation.
Stage 6 established this preference for the MCP client; it applies here for the same reason.

**No key is ever an attribute of these types.** They carry messages and results. A key lives
inside a provider instance, supplied by construction, so it cannot leak into a trace by being
part of an object that gets serialised.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from agent_service.config import ProviderName, Settings

#: Who produced a message. ``tool`` carries a tool's result back to the model.
Role = Literal["system", "user", "assistant", "tool"]

#: Why the model stopped. Deliberately not vendor spellings — the loop branches on the tool
#: calls themselves, and these are for the trace.
StopReason = Literal["end_turn", "tool_use", "max_tokens"]

#: Default ceiling on tokens for one model reply. Providers may override it; the loop does not
#: care, because it bounds turns rather than tokens.
DEFAULT_MAX_TOKENS = 2048


class ProviderNotConfiguredError(RuntimeError):
    """The selected provider has no usable credentials or endpoint.

    Raised while *building* the provider, so a misconfiguration is one clear message at
    startup rather than an authentication failure partway through a conversation.
    """


@dataclass(frozen=True)
class ToolCallRequest:
    """A tool call the model asked for, with arguments already parsed.

    ``id`` is what a tool result must reference to be matched to this call. It is required
    rather than optional because both major APIs return one, and a loop that invented its own
    would have to maintain a second index to stay in sync.
    """

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Message:
    """One conversation turn in provider-neutral form.

    A single type with optional fields rather than a class hierarchy, because the four roles
    differ only in which fields are populated, and a hierarchy would push that branching into
    every provider's translation.
    """

    role: Role
    content: str = ""
    #: Populated on an ``assistant`` message that requested tools.
    tool_calls: tuple[ToolCallRequest, ...] = ()
    #: Populated on a ``tool`` message, referencing the call it answers.
    tool_call_id: str | None = None
    #: The tool's name on a ``tool`` message; some APIs require it alongside the id.
    name: str | None = None

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str = "", tool_calls: Sequence[ToolCallRequest] = ()) -> Message:
        return cls(role="assistant", content=content, tool_calls=tuple(tool_calls))

    @classmethod
    def tool_result(cls, call: ToolCallRequest, content: str) -> Message:
        """A tool's result, addressed back to the call that asked for it."""
        return cls(role="tool", content=content, tool_call_id=call.id, name=call.name)


@dataclass(frozen=True)
class ToolSchema:
    """A tool as it is offered to the model.

    Mirrors the MCP server's published schema rather than restating it, so the description a
    model reads is the one the server wrote — the place where Stage 5 put the semantics a
    model needs in order to use a tool correctly.
    """

    name: str
    description: str
    input_schema: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Usage:
    """Token counts for the trace. Never used for control flow."""

    input_tokens: int = 0
    output_tokens: int = 0
    #: Some providers report a cache-hit count; carried for the trace, ignored otherwise.
    cached_input_tokens: int = 0


@dataclass(frozen=True)
class AssistantTurn:
    """One model reply."""

    text: str = ""
    tool_calls: tuple[ToolCallRequest, ...] = ()
    usage: Usage = field(default_factory=Usage)
    stop_reason: StopReason = "end_turn"

    @property
    def wants_tools(self) -> bool:
        """Whether the loop should execute tools and continue.

        Keyed on the presence of calls rather than on ``stop_reason``, because providers
        disagree about which reason accompanies a tool call, and the calls themselves are
        unambiguous.
        """
        return bool(self.tool_calls)


@runtime_checkable
class ModelProvider(Protocol):
    """What the loop requires of a model.

    Deliberately one method. Anything more — streaming, embeddings, a token counter — would
    be a requirement every provider has to satisfy for a feature the loop does not use.
    """

    @property
    def name(self) -> ProviderName:
        """Which provider this is, for the trace and for the provider list."""
        ...

    async def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
        *,
        budget: int | None = None,
    ) -> AssistantTurn:
        """Produce one reply.

        ``budget`` is the remaining *turn* allowance, not a token ceiling. A provider may use
        it to ask for brevity; a provider that ignores it is still correct, because the loop
        enforces its own ceiling.
        """
        ...


def build_provider(settings: Settings) -> ModelProvider:
    """Construct the provider the settings select.

    Imports are inside the branches so that selecting ``scripted`` does not import the
    Anthropic, OpenAI and boto3 SDKs. That keeps a credential-free run genuinely free of those
    dependencies at runtime, not merely free of keys.
    """
    selected = settings.agent_model_provider

    if not settings.selected_provider_is_usable():
        raise ProviderNotConfiguredError(
            f"provider {selected!r} is not configured; configured providers: "
            f"{', '.join(settings.configured_providers())}"
        )

    # Only the credential-free providers are wired so far. The SDK-backed three raise a
    # message naming themselves rather than being imported from modules that do not exist:
    # an unresolved import would fail before this function could explain anything, and a
    # tree that does not lint or type-check is not a state worth committing to.
    #
    # Each branch annotates its result explicitly, because mypy would otherwise see a
    # lazily imported symbol as Any and lose the check that a provider satisfies the
    # protocol it claims to.
    provider: ModelProvider

    if selected == "scripted":
        from .scripted import ScriptedProvider

        provider = ScriptedProvider()
    elif selected == "replay":
        from .replay import ReplayProvider

        provider = ReplayProvider.from_settings(settings)
    else:
        raise ProviderNotConfiguredError(
            f"provider {selected!r} is selected and configured, but its implementation is "
            f"not written yet; the providers available today are 'scripted' and 'replay'"
        )

    return provider
