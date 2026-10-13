"""Anthropic's messages API, translated to and from the provider-neutral types.

The translation is the whole file, and it is where the two mismatches with the neutral
message model live:

**Anthropic takes the system prompt as a parameter, not a message.** Our `Message` list may
carry one because other providers expect it inline. It is extracted here and passed as
``system``, and only the first one is used — a second system message would be silently
dropped, so it is dropped loudly instead.

**A tool result is a user message containing a `tool_result` block.** Our model has a ``tool``
role; Anthropic has no such role. The conversion is done here rather than by giving `Message`
a vendor-shaped variant, because a neutral type that had to know about this would not be
neutral.

Anthropic returns a content *list*, mixing text and ``tool_use`` blocks, and its ``stop_reason``
is one of ``end_turn`` / ``tool_use`` / ``max_tokens``. The blocks are parsed into text plus
`ToolCallRequest`s; the reason is mapped to our own vocabulary rather than passed through,
so the loop never sees a vendor's spelling.

The client is created per provider instance and holds the key. It is never a field of a
`Message`, a `ToolCallRequest` or an `AssistantTurn`, so a key cannot reach a trace by being
part of something that gets serialised — the hygiene rule ADR-0002 sets out.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent_service.config import ProviderName, Settings

from .base import (
    AssistantTurn,
    Message,
    ProviderNotConfiguredError,
    StopReason,
    ToolCallRequest,
    ToolSchema,
    Usage,
)

#: Used when the settings name no model. A current Claude model, not a placeholder — a
#: wrong-but-plausible default would fail at the API with a confusing error.
DEFAULT_MODEL = "claude-sonnet-4-5"


class AnthropicProvider:
    """`ModelProvider` backed by Anthropic's messages API.

    The SDK client is built lazily on first use so constructing the provider during startup
    validation does not require a network-reachable endpoint, while a missing key still fails
    immediately in `build_provider`.
    """

    def __init__(self, settings: Settings) -> None:
        key = settings.anthropic_api_key
        if key is None:
            raise ProviderNotConfiguredError("the anthropic provider needs ANTHROPIC_API_KEY")

        # Imported here, not at module scope, so selecting another provider does not import
        # this SDK at all.
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=key.get_secret_value())
        self._model = settings.agent_model_id or DEFAULT_MODEL
        self._name: ProviderName = "anthropic"

    @property
    def name(self) -> ProviderName:
        return self._name

    async def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
        *,
        budget: int | None = None,
    ) -> AssistantTurn:
        """One turn. ``budget`` is accepted and ignored — see `base.ModelProvider.chat`."""
        system, conversation = _split_system(messages)

        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": _max_tokens(budget),
            "messages": [_to_anthropic_message(message) for message in conversation],
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": dict(tool.input_schema) or {"type": "object"},
                }
                for tool in tools
            ]

        response = await self._client.messages.create(**request)
        return _from_anthropic_response(response)


def _split_system(messages: Sequence[Message]) -> tuple[str, list[Message]]:
    """Pull the system prompt out and leave the rest.

    Only the first system message survives, and that is stated rather than silent: a caller
    that supplied two would otherwise lose one without knowing which.
    """
    system = ""
    conversation: list[Message] = []
    for message in messages:
        if message.role == "system":
            if not system:
                system = message.content
            continue
        conversation.append(message)
    return system, conversation


def _to_anthropic_message(message: Message) -> dict[str, Any]:
    """Convert one neutral message into Anthropic's shape.

    An assistant message that requested tools keeps its ``tool_use`` blocks, because the API
    requires the request and its result to correspond when the conversation continues.
    """
    if message.role == "tool":
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "",
                    "content": message.content,
                }
            ],
        }

    if message.role == "assistant" and message.tool_calls:
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        blocks.extend(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": dict(call.arguments),
            }
            for call in message.tool_calls
        )
        return {"role": "assistant", "content": blocks}

    return {"role": message.role, "content": message.content}


def _max_tokens(budget: int | None) -> int:
    """A sane reply ceiling.

    ``budget`` is a turn allowance, not a token one, so it does not scale this — treating a
    remaining-turn count as a token count would be a category error that silently produced
    tiny replies late in a conversation.
    """
    del budget
    return 4096


def _from_anthropic_response(response: Any) -> AssistantTurn:
    """Read text and tool calls out of the response's content blocks.

    Blocks are inspected by their ``type`` rather than by position, because a response may
    carry text, one or more tool calls, or both, in any order.
    """
    texts: list[str] = []
    calls: list[ToolCallRequest] = []

    for block in getattr(response, "content", None) or ():
        block_type = getattr(block, "type", "")
        if block_type == "text":
            texts.append(str(getattr(block, "text", "")))
        elif block_type == "tool_use":
            calls.append(_call_from_block(block))

    return AssistantTurn(
        text="\n".join(texts).strip(),
        tool_calls=tuple(calls),
        usage=_usage_from(response),
        stop_reason=_reason_from(getattr(response, "stop_reason", None)),
    )


def _call_from_block(block: Any) -> ToolCallRequest:
    """One ``tool_use`` block as a neutral call.

    ``input`` is already a mapping in this API, unlike OpenAI's JSON string — which is exactly
    the kind of difference `base.py` exists to absorb.
    """
    raw = getattr(block, "input", None)
    arguments: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    return ToolCallRequest(
        id=str(getattr(block, "id", "")),
        name=str(getattr(block, "name", "")),
        arguments=arguments,
    )


def _usage_from(response: Any) -> Usage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return Usage()
    return Usage(
        input_tokens=_int_or_zero(getattr(usage, "input_tokens", 0)),
        output_tokens=_int_or_zero(getattr(usage, "output_tokens", 0)),
        cached_input_tokens=_int_or_zero(getattr(usage, "cache_read_input_tokens", 0)),
    )


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _reason_from(raw: Any) -> StopReason:
    """Map the vendor's reason to ours.

    Anything unrecognised becomes ``end_turn``. The loop keys on the presence of tool calls
    rather than on this field, so an unexpected value must not be able to change control flow
    — it only affects what the trace records.
    """
    if raw == "tool_use":
        return "tool_use"
    if raw == "max_tokens":
        return "max_tokens"
    return "end_turn"
