"""OpenAI's chat-completions API — and, unchanged, every OpenAI-shaped endpoint.

One class serves both ``openai`` and ``openai_compatible`` because there is nothing to
change: Ollama, vLLM, LM Studio, Groq and Together all speak this protocol, and the only
difference is a base URL. ADR-0002 makes local inference a first-class option on that basis,
and duplicating the class per vendor would be duplication with no behavioural difference.

Three differences from Anthropic, and each is a translation rather than a design:

**The system prompt stays inline.** OpenAI has a ``system`` role, so our system message maps
straight through — no extraction, unlike the Anthropic provider.

**Tool arguments arrive as a JSON *string*.** This is the one that matters. ``function.
arguments`` is a string that must be parsed, and a model can emit malformed JSON. A parse
failure becomes a `ToolCallRequest` with empty arguments and the raw string preserved under
``_raw_arguments``, so the loop sees a call it can reject with a clear reason rather than a
crash inside a provider. Silently substituting ``{}`` would be worse: the tool would run with
missing required arguments and fail somewhere less informative.

**``finish_reason`` is ``tool_calls``, not ``tool_use``.** Mapped to our vocabulary, with
anything unrecognised becoming ``end_turn``, for the same reason as in the Anthropic provider:
the loop keys on the calls themselves.
"""

from __future__ import annotations

import json
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

#: Key under which an unparseable argument string is preserved. Prefixed so it cannot collide
#: with a parameter a tool actually declares.
RAW_ARGUMENTS_KEY = "_raw_arguments"

#: Used when the settings name no model. Chosen for broad availability: a local
#: OpenAI-shaped server is unlikely to serve this exact id, which is why
#: ``openai_compatible`` deployments are expected to set AGENT_MODEL_ID.
DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIProvider:
    """`ModelProvider` backed by chat completions, over any compatible endpoint."""

    def __init__(self, settings: Settings) -> None:
        key = settings.openai_api_key
        base_url = settings.openai_base_url.strip()

        # A local endpoint usually needs no key, while the hosted API needs one. Requiring
        # the key only when there is no base URL is what makes both cases work without an
        # operator having to supply a dummy secret to a local server.
        if key is None and not base_url:
            raise ProviderNotConfiguredError(
                "the openai provider needs OPENAI_API_KEY, or OPENAI_BASE_URL for a local "
                "OpenAI-compatible endpoint that does not require one"
            )

        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            api_key=key.get_secret_value() if key is not None else "not-needed",
            base_url=base_url or None,
        )
        self._model = settings.agent_model_id or DEFAULT_MODEL
        #: Whether this instance is pointed at a local endpoint. Reported for the trace, and
        #: it is the reason the key was optional.
        self._local = bool(base_url) and key is None
        self._name: ProviderName = "openai_compatible" if base_url else "openai"

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
        request: dict[str, Any] = {
            "model": self._model,
            "messages": [_to_openai_message(message) for message in messages],
        }
        if tools:
            request["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.input_schema) or {"type": "object"},
                    },
                }
                for tool in tools
            ]

        response = await self._client.chat.completions.create(**request)
        return _from_openai_response(response)


def _to_openai_message(message: Message) -> dict[str, Any]:
    """Convert one neutral message into OpenAI's shape.

    An assistant message that requested tools must repeat those calls, because the API
    requires each ``tool`` message to answer a call the assistant actually made.
    """
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id or "",
            "content": message.content,
        }

    if message.role == "assistant" and message.tool_calls:
        return {
            "role": "assistant",
            # ``content`` may legitimately be null here, which is what the API expects when
            # the turn is only tool calls.
            "content": message.content or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(dict(call.arguments)),
                    },
                }
                for call in message.tool_calls
            ],
        }

    return {"role": message.role, "content": message.content}


def _from_openai_response(response: Any) -> AssistantTurn:
    """Read the first choice.

    Only the first: this loop asks for one reply per turn and never sets ``n``. Reading a
    second choice would produce two assistant turns for one request and the transcript would
    no longer correspond to what the model was asked.
    """
    choices = getattr(response, "choices", None) or ()
    if not choices:
        # A response with no choices is a provider fault, not an empty answer. Returning an
        # empty turn would end the conversation quietly and look like the model had nothing to
        # add.
        return AssistantTurn(
            text="the model returned no choices",
            stop_reason="end_turn",
            usage=_usage_from(response),
        )

    choice = choices[0]
    message = getattr(choice, "message", None)

    return AssistantTurn(
        text=str(getattr(message, "content", "") or ""),
        tool_calls=tuple(_calls_from(message)),
        usage=_usage_from(response),
        stop_reason=_reason_from(getattr(choice, "finish_reason", None)),
    )


def _calls_from(message: Any) -> list[ToolCallRequest]:
    raw_calls = getattr(message, "tool_calls", None) or ()
    return [call for call in (_call_from(raw) for raw in raw_calls) if call is not None]


def _call_from(raw: Any) -> ToolCallRequest | None:
    """One tool call, with its argument string parsed.

    Returns ``None`` for a call missing an id or a function name, because such a call cannot
    be answered — a tool result must reference an id. Dropping it means the loop sees fewer
    calls rather than an uncallable one.
    """
    call_id = str(getattr(raw, "id", "") or "")
    function = getattr(raw, "function", None)
    name = str(getattr(function, "name", "") or "")
    if not call_id or not name:
        return None

    return ToolCallRequest(
        id=call_id,
        name=name,
        arguments=_parse_arguments(getattr(function, "arguments", "")),
    )


def _parse_arguments(raw: Any) -> Mapping[str, Any]:
    """Parse the argument string, preserving it when it is not valid JSON.

    The raw text is kept under ``_raw_arguments`` so a caller can report *what the model
    actually sent* rather than "invalid arguments" with nothing to show. Substituting an empty
    mapping and discarding the text would make a tool execute with missing parameters and fail
    somewhere that cannot explain why.
    """
    if isinstance(raw, Mapping):
        # Some compatible endpoints return an object where the API returns a string.
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {RAW_ARGUMENTS_KEY: raw}

    # A valid JSON scalar (a bare string or number) is still not an argument mapping.
    return parsed if isinstance(parsed, Mapping) else {RAW_ARGUMENTS_KEY: raw}


def _usage_from(response: Any) -> Usage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return Usage()

    details = getattr(usage, "prompt_tokens_details", None)
    return Usage(
        input_tokens=_int_or_zero(getattr(usage, "prompt_tokens", 0)),
        output_tokens=_int_or_zero(getattr(usage, "completion_tokens", 0)),
        cached_input_tokens=_int_or_zero(getattr(details, "cached_tokens", 0)),
    )


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _reason_from(raw: Any) -> StopReason:
    """Map ``finish_reason`` into our vocabulary.

    Anything unrecognised, including ``length`` and ``content_filter``, becomes ``end_turn``:
    the loop keys on the presence of tool calls, and a mapped reason must not be able to
    change control flow — only what the trace records.
    """
    if raw == "tool_calls":
        return "tool_use"
    if raw == "length":
        return "max_tokens"
    return "end_turn"
