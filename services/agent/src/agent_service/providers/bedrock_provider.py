"""Amazon Bedrock's Converse API, translated to and from the provider-neutral types.

The interesting difference from the other two SDK providers is not the message format — it is
that **boto3 is synchronous**. There is no async Bedrock client, so every call is dispatched
with ``asyncio.to_thread``. Calling it directly would block the event loop for the duration of
a model round trip, which in this service means ``GET /ping`` stops answering while a turn is
in flight and the runtime sees a dead instance rather than a busy one. That is a real failure
mode, not a theoretical one, and it is why the dispatch is here rather than left implicit.

Two format notes. Converse takes the system prompt as its own ``system`` list, like Anthropic
and unlike OpenAI. And its tool schema is nested under ``toolSpec.inputSchema.json``, one level
deeper than the other two — a shape difference the `base.ToolSchema` type exists to absorb.

Converse's ``stopReason`` is already close to our vocabulary (``end_turn`` / ``tool_use`` /
``max_tokens``), so the mapping is nearly the identity. It is still written out, because
"nearly" is how a vendor's spelling ends up leaking into the loop.
"""

from __future__ import annotations

import asyncio
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

#: Used when the settings name no model. A current, broadly available Claude model on
#: Bedrock; ``BEDROCK_MODEL_ID`` overrides it because ids are region- and account-specific.
DEFAULT_MODEL = "anthropic.claude-3-5-sonnet-20241022-v2:0"


class BedrockProvider:
    """`ModelProvider` backed by Bedrock Converse, dispatched off the event loop."""

    def __init__(self, settings: Settings) -> None:
        region = settings.aws_region.strip()
        if not region:
            raise ProviderNotConfiguredError(
                "the bedrock provider needs AWS_REGION so boto3 knows which endpoint to use"
            )

        # boto3 ships no type information, so there is nothing here for mypy to check. This is
        # a real gap in the dependency rather than an inconvenience: the suppression is scoped
        # to the import, and everything this module does with the client is still checked
        # against the shapes the API returns.
        from boto3 import client  # type: ignore[import-untyped]

        self._client = client("bedrock-runtime", region_name=region)
        self._model = settings.bedrock_model_id or settings.agent_model_id or DEFAULT_MODEL
        self._name: ProviderName = "bedrock"

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
        """One turn, run on a worker thread because boto3 is synchronous."""
        system, conversation = _split_system(messages)

        request: dict[str, Any] = {
            "modelId": self._model,
            "messages": [_to_converse_message(message) for message in conversation],
        }
        if system:
            request["system"] = [{"text": system}]
        if tools:
            request["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": tool.name,
                            "description": tool.description,
                            "inputSchema": {"json": dict(tool.input_schema) or {"type": "object"}},
                        }
                    }
                    for tool in tools
                ]
            }

        response = await asyncio.to_thread(self._client.converse, **request)
        return _from_converse_response(response)


def _split_system(messages: Sequence[Message]) -> tuple[str, list[Message]]:
    """Pull the system prompt out; only the first one survives, as with Anthropic."""
    system = ""
    conversation: list[Message] = []
    for message in messages:
        if message.role == "system":
            if not system:
                system = message.content
            continue
        conversation.append(message)
    return system, conversation


def _to_converse_message(message: Message) -> dict[str, Any]:
    """Convert one neutral message into Converse's shape.

    Content is always a list of typed blocks — Converse has no plain-string form — and a tool
    result is a ``toolResult`` block inside a **user** message, because Converse, like
    Anthropic, has no tool role.
    """
    if message.role == "tool":
        return {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": message.tool_call_id or "",
                        "content": [{"text": message.content}],
                    }
                }
            ],
        }

    blocks: list[dict[str, Any]] = []
    if message.content:
        blocks.append({"text": message.content})
    blocks.extend(
        {
            "toolUse": {
                "toolUseId": call.id,
                "name": call.name,
                "input": dict(call.arguments),
            }
        }
        for call in message.tool_calls
    )

    # An assistant turn that is only tool calls still needs a non-empty content list, which is
    # why the blocks are built rather than the text being passed through.
    if not blocks:
        blocks.append({"text": ""})

    return {"role": message.role, "content": blocks}


def _from_converse_response(response: Any) -> AssistantTurn:
    """Read text and tool calls out of ``output.message.content``.

    The response is nested three levels deep, and each level may be absent on an error-shaped
    payload — hence the chained ``getattr`` rather than direct indexing.
    """
    output = getattr(response, "output", None) or {}
    message = _mapping_get(output, "message") or {}
    blocks = _mapping_get(message, "content") or ()

    texts: list[str] = []
    calls: list[ToolCallRequest] = []

    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        if isinstance(text := block.get("text"), str):
            texts.append(text)
        elif isinstance(tool_use := block.get("toolUse"), Mapping):
            calls.append(_call_from(tool_use))

    return AssistantTurn(
        text="\n".join(texts).strip(),
        tool_calls=tuple(calls),
        usage=_usage_from(response),
        stop_reason=_reason_from(_mapping_get(response, "stopReason")),
    )


def _call_from(tool_use: Mapping[str, Any]) -> ToolCallRequest:
    """One ``toolUse`` block as a neutral call.

    ``input`` is already an object here, as with Anthropic — but it is not guaranteed to be a
    mapping on every model, so it is checked rather than assumed.
    """
    raw_input = tool_use.get("input")
    arguments: Mapping[str, Any] = raw_input if isinstance(raw_input, Mapping) else {}
    return ToolCallRequest(
        id=str(tool_use.get("toolUseId", "")),
        name=str(tool_use.get("name", "")),
        arguments=arguments,
    )


def _usage_from(response: Any) -> Usage:
    usage = _mapping_get(response, "usage")
    if not isinstance(usage, Mapping):
        return Usage()
    return Usage(
        input_tokens=_int_or_zero(usage.get("inputTokens")),
        output_tokens=_int_or_zero(usage.get("outputTokens")),
        cached_input_tokens=_int_or_zero(usage.get("cacheReadInputTokens")),
    )


def _mapping_get(source: Any, key: str) -> Any:
    """Read ``key`` from a mapping-ish object, tolerating a non-mapping.

    boto3 returns plain dicts, but a stubbed or proxied client may not, and the cost of a
    defensive read here is far lower than an AttributeError raised from inside a provider.
    """
    return source.get(key) if isinstance(source, Mapping) else None


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _reason_from(raw: Any) -> StopReason:
    """Map ``stopReason`` into our vocabulary.

    Nearly the identity, and written out anyway: leaving a vendor's spelling in place is how
    the loop slowly acquires a dependency on one provider's strings.
    """
    if raw == "tool_use":
        return "tool_use"
    if raw == "max_tokens":
        return "max_tokens"
    return "end_turn"
