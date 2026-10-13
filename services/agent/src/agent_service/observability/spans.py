"""One unit of work in a trace.

A span is deliberately a plain, serialisable record rather than an object holding references to
a provider, a tool or a timer. Traces are read months after the process that wrote them is gone,
so a span must survive as JSON with no code to interpret it — and `jq` has to be enough to read
it at 2am.

**Timing is stored, not computed on read.** ``started_at``/``ended_at`` are wall-clock instants
and ``duration_ms`` is the difference, kept as well. Storing both looks redundant until a trace
crosses a clock adjustment: the timestamps then say one thing and the measured duration another,
and having both makes the discrepancy visible instead of silently choosing one.

**Attributes are flat strings and numbers.** No nested objects, because a trace file is read by
tools that were not written for it — a nested shape forces every reader to know the schema, and
the whole point of the JSONL file is that it does not.

Spans nest by name and order rather than by explicit parent pointers. A trace is a sequence of
events from one invocation; a tree would be accurate and would also make "show me this run in
order" a traversal. Order is the primary access pattern, so it is what the format is built for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: What kind of work a span records. Closed rather than free text, so a filter is meaningful and
#: a new kind is a deliberate change to this set rather than a typo in a call site.
SpanKind = str

#: The kinds in use. Exported so a reader can see the vocabulary without grepping call sites.
MODEL_TURN: SpanKind = "model_turn"
TOOL_CALL: SpanKind = "tool_call"
PROPOSAL: SpanKind = "proposal"
EXECUTION: SpanKind = "execution"
MEMORY: SpanKind = "memory"

ALL_KINDS: frozenset[str] = frozenset({MODEL_TURN, TOOL_CALL, PROPOSAL, EXECUTION, MEMORY})


@dataclass
class Span:
    """One event in a run, ready to be written as one JSON line.

    ``attributes`` holds only scalars. A tool's arguments are recorded as their *rendering*
    (the same string the user would see) rather than as a nested object, which keeps the file
    flat and keeps a trace from becoming a place where payloads accumulate unread.
    """

    kind: SpanKind
    name: str
    started_at: datetime
    ended_at: datetime | None = None
    #: The caller's own fields. Values are strings, numbers or booleans — see the module docstring.
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int | None:
        """The measured elapsed time, or ``None`` while the span is open.

        ``None`` rather than 0 for an open span: zero is a real duration and an unfinished span
        does not have one, and a trace that reports 0ms for something still running is worse than
        one that reports nothing.
        """
        if self.ended_at is None:
            return None
        return int((self.ended_at - self.started_at).total_seconds() * 1000)

    def as_dict(self) -> dict[str, Any]:
        """The row this span becomes in the trace file.

        Keys are fixed and always present, including the optional ones. A reader that has to
        handle a missing ``ended_at`` key and an ``ended_at`` of ``None`` differently would be a
        reader that breaks on the first interrupted run — which is exactly the run someone is
        looking at when they open the file.
        """
        return {
            "kind": self.kind,
            "name": self.name,
            "started_at": self.started_at.isoformat(),
            "ended_at": None if self.ended_at is None else self.ended_at.isoformat(),
            "duration_ms": self.duration_ms,
            "attributes": {key: _scalar(value) for key, value in self.attributes.items()},
        }


def _scalar(value: Any) -> Any:
    """Coerce one attribute value to something JSON can hold flat.

    Anything that is not a string, number, boolean or ``None`` becomes ``repr`` of itself. That
    is lossy on purpose: the alternative is a nested object in a file whose reader does not know
    the schema, and a trace that cannot be read with ``jq`` has lost the property it exists for.
    A structure worth preserving belongs in its own span, not in an attribute.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)
