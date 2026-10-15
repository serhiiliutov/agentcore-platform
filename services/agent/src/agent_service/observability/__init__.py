"""Traces: what an invocation did, in order, readable after the fact.

The assignment asks that any single run be explainable afterwards. That requirement splits into
two things, and this package does only the first:

* **Recording** — a run's spans go to a JSONL file, and one summary row goes to the database.
  That is here.
* **Serving** — the read side that lists and opens traces is the repository and, above it, the
  API. Not here, because a recorder that also knows how a caller wants to browse is a recorder
  nobody can test without a database.
* **Exporting** — `otlp.py` maps a finished run onto the OpenTelemetry wire format, so a collector
  can read it. Off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set, and it cannot fail a turn.

Spans are written once, at the end of a run, into a temporary file that is then moved into place.
A reader therefore never opens a partial trace and mistakes it for a complete one — which is the
specific way a trace becomes worse than useless, because it looks trustworthy.
"""

from __future__ import annotations

from .otlp import RunExport
from .spans import (
    ALL_KINDS,
    EXECUTION,
    MEMORY,
    MODEL_TURN,
    PROPOSAL,
    TOOL_CALL,
    Span,
)
from .tracer import Tracer, WriteFailureCallback

__all__ = [
    "ALL_KINDS",
    "EXECUTION",
    "MEMORY",
    "MODEL_TURN",
    "PROPOSAL",
    "TOOL_CALL",
    "RunExport",
    "Span",
    "Tracer",
    "WriteFailureCallback",
]
