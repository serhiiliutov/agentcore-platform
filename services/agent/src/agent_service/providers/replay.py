"""Replays a recorded live run — the third way to get a deterministic conversation.

ADR-0002 distinguishes this from `scripted` for a reason worth restating, because the
difference is what makes both worth having. `scripted` *manufactures* a conversation from
rules: it decides. `replay` *reproduces* one that a real model produced: it obeys.

That matters for a specific failure. `scripted` can prove the loop handles a tool call the
loop's author thought of. It cannot prove the loop handles what a real model actually emits —
a tool call with an unexpected argument, two tool calls in one turn, an empty content block
alongside a call, a `stop_reason` that disagrees with the calls. A recorded run carries those
shapes verbatim, so the loop is tested against reality rather than against its author's
imagination, still with no key, no network and no cost.

A recording is a JSON array of `AssistantTurn`s in order. Exhausting it raises rather than
looping back to the start: a replayed run that silently restarts would make a loop that never
terminates look like a loop that terminates, which is precisely the bug this provider exists
to catch.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_service.config import REPLAY_DEFAULT_FILENAME, ProviderName, Settings

from .base import (
    AssistantTurn,
    Message,
    ProviderNotConfiguredError,
    StopReason,
    ToolCallRequest,
    ToolSchema,
    Usage,
)

#: Where a recording is expected when the setting names a directory rather than a file.
#:
#: An alias for the constant `agent_service.config` owns, not a second literal. `Settings` has to
#: resolve a directory exactly this way to decide whether `replay` is offered at all, and two
#: copies of the name would be two chances to disagree about which file a recording is — with the
#: symptom landing on a user, as "offered by the picker but rejected when chosen".
DEFAULT_FILENAME = REPLAY_DEFAULT_FILENAME


class RecordingExhaustedError(RuntimeError):
    """The conversation asked for more turns than the recording contains.

    Deliberately not a quiet restart. A replay that wrapped around would hide a
    non-terminating loop — the loop would appear to keep producing turns, when in truth it
    was consuming the same recording repeatedly.
    """


@dataclass
class ReplayProvider:
    """Yields recorded turns in order, ignoring what it is asked.

    ``turns`` is the whole recording. The messages and tools arguments are accepted and
    ignored, for the same reason as `scripted`: the point is to reproduce a fixed sequence, so
    letting the conversation influence the output would defeat it.
    """

    turns: tuple[AssistantTurn, ...] = ()
    #: How many turns have been yielded, exposed for assertions.
    turns_taken: int = 0
    _name: ProviderName = field(default="replay", init=False)

    @property
    def name(self) -> ProviderName:
        return self._name

    @classmethod
    def from_settings(cls, settings: Settings) -> ReplayProvider:
        """Load the recording the settings point at.

        ``agent_model_id`` names the file when replaying, because a replay run's "model" is
        the recording — it is the thing whose identity a trace needs to record.
        """
        raw = settings.agent_model_id.strip()
        if not raw:
            raise ProviderNotConfiguredError(
                "the replay provider needs a recording; set AGENT_MODEL_ID to the JSON file "
                "to replay"
            )

        path = Path(raw)
        if path.is_dir():
            path = path / DEFAULT_FILENAME
        if not path.exists():
            raise ProviderNotConfiguredError(f"recording not found: {path}")

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProviderNotConfiguredError(f"recording {path} is not valid JSON: {exc}") from exc

        return cls(turns=tuple(_turn_from(item) for item in _as_list(payload, path)))

    async def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
        *,
        budget: int | None = None,
    ) -> AssistantTurn:
        """Return the next recorded turn, or refuse to invent one."""
        if self.turns_taken >= len(self.turns):
            raise RecordingExhaustedError(
                f"the recording has {len(self.turns)} turn(s) and the conversation asked for "
                f"more; the loop did not terminate within the recording"
            )
        turn = self.turns[self.turns_taken]
        self.turns_taken += 1
        return turn


def _as_list(payload: Any, path: Path) -> list[Any]:
    """A recording is an array of turns; anything else is a malformed recording."""
    if not isinstance(payload, list):
        raise ProviderNotConfiguredError(
            f"recording {path} must be a JSON array of turns, got {type(payload).__name__}"
        )
    return payload


def _turn_from(item: Any) -> AssistantTurn:
    """Build a turn from one recorded object.

    Field lookups are tolerant of a missing key and strict about a wrong type, because a
    recording is a fixture: a missing field is an omission the defaults cover, while a string
    where a list belongs means the recording and the reader disagree about the format, and
    that should fail now rather than mid-conversation.
    """
    if not isinstance(item, dict):
        raise ProviderNotConfiguredError(
            f"each recorded turn must be an object, got {type(item).__name__}"
        )

    reason = item.get("stop_reason", "end_turn")
    if not isinstance(reason, str):
        raise ProviderNotConfiguredError(
            f"stop_reason must be a string, got {type(reason).__name__}"
        )

    return AssistantTurn(
        text=str(item.get("text", "")),
        tool_calls=tuple(_call_from(raw) for raw in _calls(item.get("tool_calls"))),
        usage=_usage_from(item.get("usage")),
        # A recording with an unexpected reason is treated as a plain end of turn rather than
        # rejected: the reason is for the trace, and the loop keys on the calls.
        stop_reason=_known_reason(reason),
    )


def _calls(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ProviderNotConfiguredError(
            f"tool_calls must be an array when present, got {type(raw).__name__}"
        )
    return raw


def _call_from(raw: Any) -> ToolCallRequest:
    if not isinstance(raw, dict):
        raise ProviderNotConfiguredError(
            f"each recorded tool call must be an object, got {type(raw).__name__}"
        )
    arguments = raw.get("arguments", {})
    return ToolCallRequest(
        id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        arguments=arguments if isinstance(arguments, dict) else {},
    )


def _usage_from(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    return Usage(
        input_tokens=_int_or_zero(raw.get("input_tokens")),
        output_tokens=_int_or_zero(raw.get("output_tokens")),
        cached_input_tokens=_int_or_zero(raw.get("cached_input_tokens")),
    )


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _known_reason(value: str) -> StopReason:
    if value in ("end_turn", "tool_use", "max_tokens"):
        return value  # type: ignore[return-value]
    return "end_turn"
