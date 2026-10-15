"""Exporting a run's spans to an OpenTelemetry collector.

The traces this service records locally are JSONL files plus a row in `traces`. That is enough to
answer "what did this run do", and it is deliberately not enough to answer "what is this deployment
doing" — for that, spans have to leave the machine, and the interoperable way to send them is OTLP.

**Why this is written here rather than configured from the SDK.** The OpenTelemetry SDK is not a
dependency of this workspace, and adding it means pulling protobuf and a gRPC stack to make one
POST. What this project actually needs from OTLP is the *mapping*: run and span records that already
exist, turned into the wire shape a collector understands. That mapping is about a page of code, it
is the part worth reviewing, and doing it explicitly means a test can assert the exact document
without a collector running. `httpx` is already a dependency, so the transport costs nothing new.

**What is given up, stated plainly.** No batching, retries, sampling, or resource detection, and
HTTP/JSON only — no gRPC. The first three are deliberate rather than pending: spans are written once
per invocation (not per request), a tracing failure must never delay or fail a turn, and sampling is
a decision for the collector. And the default is off: with `OTEL_EXPORTER_OTLP_ENDPOINT` empty,
nothing in this module runs, which is what keeps a bare `docker compose up` a local, offline stack.

**How to try it.** Point the endpoint at a collector that accepts OTLP/HTTP and run anything:

    docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one:latest
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 devtasks serve

with `OTEL_SERVICE_NAME` naming this service (`agentcore-platform` by default), since that is the
attribute a collector groups by.

**GenAI semantic conventions.** Model and tool spans carry the attribute names the convention
defines (`gen_ai.operation.name`, `gen_ai.system`, `gen_ai.request.model`, `gen_ai.tool.name`), so a
collector that understands them renders a run as model and tool calls rather than as opaque spans.
The conventions are still evolving; the names are constants here, in one place, because they are the
contract with whatever reads the trace.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from .spans import MODEL_TURN, TOOL_CALL, Span

logger = logging.getLogger(__name__)

#: The path OTLP/HTTP carries traces on. Appended to the configured endpoint unless it is already
#: there, because the OpenTelemetry specification's variable names a *base* URL in some tools and
#: the full path in others, and both are in the wild.
TRACES_PATH = "/v1/traces"

#: `SpanKind` and `Status` values from the OTLP protocol. INTERNAL for every span, because a span
#: emitted from inside a library is neither a client nor a server span, and claiming otherwise
#: would draw a misleading edge in a trace viewer.
SPAN_KIND_INTERNAL = 1
STATUS_OK = 1
STATUS_ERROR = 2

#: The default timeout for one export. Short, and it is a tracing side-channel: a collector that is
#: unreachable must not hold a turn open.
DEFAULT_TIMEOUT_S = 5.0

GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_OPERATION = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
SESSION_ID = "session.id"
ENDUSER_ID = "enduser.id"

#: The names the GenAI conventions give these operations. `chat` and `execute_tool` are the two
#: with defined names; the rest of ours are internal phases, so they are reported by kind.
_OPERATION_CHAT = "chat"
_OPERATION_TOOL = "execute_tool"
_OPERATION_INVOKE = "invoke_agent"

#: Kinds whose name is worth mapping to a convention. Everything else keeps its own kind as the
#: operation, which is honest: `proposal` and `memory` are this project's vocabulary, not OTel's.
_KIND_OPERATIONS: dict[str, str] = {
    MODEL_TURN: _OPERATION_CHAT,
    TOOL_CALL: _OPERATION_TOOL,
}

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class RunExport:
    """One run, as the values an exporter needs.

    A flat record rather than the `Tracer` itself, so this module cannot reach back into a live
    object: what is exported is what the run *was*, and a field nobody fills in says so by being
    absent from this dataclass rather than by being a method that happens to return something else
    later.
    """

    trace_id: str
    session_id: str
    user_id: str
    provider: str
    model_id: str
    status: str
    error: str | None
    started_at: datetime
    ended_at: datetime
    spans: tuple[Span, ...]

    @property
    def failed(self) -> bool:
        return self.status != "ok" or self.error is not None


def _unix_nanos(moment: datetime) -> str:
    """A timestamp as OTLP/JSON wants it: nanoseconds since the epoch, as a *string*.

    String because OTLP encodes 64-bit integers that way — a JSON number would lose precision in
    JavaScript long before it lost it here, and a timestamp that lands in the wrong second is worse
    than one that fails to parse. Integer arithmetic rather than `timestamp() * 1e9` for the same
    reason: the float rounds, and the rounding is invisible.
    """
    if moment.tzinfo is None:
        # A naive datetime is treated as UTC, which is what every clock this project uses returns.
        # Assuming local time instead would shift traces by the host's offset, which is the kind of
        # bug that looks like a collector problem.
        moment = moment.replace(tzinfo=UTC)
    delta = moment - _EPOCH
    return str((delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000)


def _namespace(trace_id: str) -> Any:
    """A UUID to derive span ids from, without requiring the trace id to be one.

    `Tracer` generates a UUID, so this normally just parses it. A trace id from somewhere else —
    a caller that supplied its own — still gets stable ids rather than an exception, because
    refusing to export a trace because its id has the wrong shape is a poor trade.
    """
    try:
        return uuid.UUID(trace_id)
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_URL, trace_id)


def _span_id(namespace: Any, *parts: str) -> str:
    """A 16-hex-character span id, derived rather than random.

    Derived so that exporting the same run twice produces the same document: a trace that changes
    identity every time it is sent cannot be deduplicated by a collector, and two exports of one
    run would appear as two runs.
    """
    return uuid.uuid5(namespace, ":".join(parts)).hex[:16]


def _attribute(key: str, value: Any) -> dict[str, Any]:
    """One OTLP attribute, typed the way the protocol types it.

    ``intValue`` is a string for the same 64-bit reason the timestamps are. Booleans are checked
    before integers because `bool` is a subclass of `int` in Python, so a naive check would send
    every boolean as a number — and a collector reading `true` as `1` is a small lie in a place
    nobody looks.
    """
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _attributes(pairs: dict[str, Any]) -> list[dict[str, Any]]:
    """Every attribute, in a stable order so two exports of one run are byte-comparable."""
    return [_attribute(key, pairs[key]) for key in sorted(pairs)]


def _status_for(*, failed: bool, error: str | None) -> dict[str, Any]:
    if not failed:
        return {"code": STATUS_OK}
    status: dict[str, Any] = {"code": STATUS_ERROR}
    if error:
        # The message goes on the status rather than only in an attribute, because a trace viewer
        # shows it beside the red span and an attribute has to be expanded to be read.
        status["message"] = error[:1024]
    return status


def _span_name(span: Span, operation: str) -> str:
    """What the span is called in a collector's list.

    The GenAI conventions name a tool span `execute_tool <name>`, which is what makes a trace
    readable at a glance — "execute_tool search_tickets" says more than "tool_call" repeated.
    """
    if operation == _OPERATION_TOOL:
        return f"{_OPERATION_TOOL} {span.name}"
    if operation == _OPERATION_CHAT:
        return _OPERATION_CHAT
    return f"{span.kind} {span.name}"


def _span_attributes(span: Span, run: RunExport, operation: str) -> dict[str, Any]:
    """The span's own attributes plus the convention's, without overwriting either.

    The span's recorded attributes are spread first so the convention keys win: a caller that
    happened to record something called `gen_ai.system` should not be able to describe the run
    wrongly, and the value this service knows is the true one.
    """
    combined: dict[str, Any] = dict(span.attributes)
    combined[GEN_AI_OPERATION] = operation
    if operation == _OPERATION_TOOL:
        combined[GEN_AI_TOOL_NAME] = span.name
    if operation == _OPERATION_CHAT:
        combined[GEN_AI_SYSTEM] = run.provider
        if run.model_id:
            combined[GEN_AI_REQUEST_MODEL] = run.model_id
    # The span's kind is carried for every span, because the operation name maps three of ours
    # onto two conventions and the original is what the local JSONL file calls it.
    combined["agentcore.span.kind"] = span.kind
    return combined


def _resource(service_name: str) -> dict[str, Any]:
    return {
        "attributes": _attributes(
            {
                "service.name": service_name,
                # Not a convention, but a fact worth carrying: without it a collector receiving
                # spans from several stages of this project sees one service.
                "service.namespace": "agentcore-platform",
            }
        )
    }


def build_payload(run: RunExport, *, service_name: str) -> dict[str, Any]:
    """The OTLP/JSON document for one run.

    A pure function, and that is the point: a test asserts the exact document — ids, nanoseconds,
    attribute typing, status — with no collector and no network. Everything above this line is
    mapping, and mapping is the part that is easy to get subtly wrong and hard to notice.

    Every span is a child of one root span for the invocation, because a run *is* one operation:
    the collector then shows one trace with the model turns and tool calls under it, rather than
    twenty unrelated spans.
    """
    namespace = _namespace(run.trace_id)
    trace_id = uuid.UUID(int=namespace.int).hex
    root_id = _span_id(namespace, "root")

    root_attributes: dict[str, Any] = {GEN_AI_OPERATION: _OPERATION_INVOKE}
    if run.session_id:
        root_attributes[SESSION_ID] = run.session_id
    if run.user_id:
        root_attributes[ENDUSER_ID] = run.user_id
    if run.provider:
        root_attributes[GEN_AI_SYSTEM] = run.provider
    if run.model_id:
        root_attributes[GEN_AI_REQUEST_MODEL] = run.model_id

    spans: list[dict[str, Any]] = [
        {
            "traceId": trace_id,
            "spanId": root_id,
            "name": _OPERATION_INVOKE,
            "kind": SPAN_KIND_INTERNAL,
            "startTimeUnixNano": _unix_nanos(run.started_at),
            "endTimeUnixNano": _unix_nanos(run.ended_at),
            "attributes": _attributes(root_attributes),
            "status": _status_for(failed=run.failed, error=run.error),
        }
    ]

    for index, span in enumerate(run.spans):
        operation = _KIND_OPERATIONS.get(span.kind, span.kind)
        # An unfinished span — the process died mid-turn — is exported with its start time and the
        # run's end. Dropping it would lose the record of what the turn was doing when it stopped,
        # which is the one thing a reader wants from a crash.
        ended_at = span.ended_at or run.ended_at
        failed = bool(span.attributes.get("failed"))
        spans.append(
            {
                "traceId": trace_id,
                # Index in the id, so two spans with the same name in one run stay distinct.
                "spanId": _span_id(namespace, str(index), span.kind, span.name),
                "parentSpanId": root_id,
                "name": _span_name(span, operation),
                "kind": SPAN_KIND_INTERNAL,
                "startTimeUnixNano": _unix_nanos(span.started_at),
                "endTimeUnixNano": _unix_nanos(ended_at),
                "attributes": _attributes(_span_attributes(span, run, operation)),
                "status": _status_for(
                    failed=failed,
                    error=str(span.attributes.get("error")) if failed else None,
                ),
            }
        )

    return {
        "resourceSpans": [
            {
                "resource": _resource(service_name),
                "scopeSpans": [
                    {
                        "scope": {"name": "agentcore-platform.agent"},
                        "spans": spans,
                    }
                ],
            }
        ]
    }


class OtlpExporter:
    """Posts one run's spans to a collector, and never fails a turn doing it.

    The same rule as the JSONL writer, for the same reason: the trace exists to explain a run, so
    refusing to complete the run because the explanation could not be delivered inverts the purpose.
    A failure is logged and reported as `False` — visible, and not fatal.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        service_name: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._service_name = service_name
        self._timeout_s = timeout_s
        # A seam for tests, and only for tests: a unit test that had to open a socket to prove the
        # exporter's error handling would be slower and flakier than the code it checks.
        self._transport = transport

    @property
    def traces_url(self) -> str:
        """Where the document is posted.

        Tolerates an endpoint that already names the path, because both spellings are documented by
        different tools and appending blindly would post to `/v1/traces/v1/traces`.
        """
        if self._endpoint.endswith(TRACES_PATH):
            return self._endpoint
        return f"{self._endpoint}{TRACES_PATH}"

    async def export(self, run: RunExport) -> bool:
        """Send one run. Returns whether the collector accepted it; never raises."""
        payload = build_payload(run, service_name=self._service_name)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.post(self.traces_url, json=payload)
        except httpx.HTTPError as exc:
            # Caught rather than propagated: an unreachable collector, a TLS failure and a timeout
            # are all "the trace did not leave the machine", and none of them is a reason to fail
            # the turn that produced it.
            logger.warning("could not export trace %s: %s", run.trace_id, exc)
            return False

        if response.status_code >= 400:
            # The body is included because a collector's rejection reason is the actionable part —
            # a 400 with no detail is the most common way an OTLP integration is wrong.
            logger.warning(
                "collector refused trace %s: %s %s",
                run.trace_id,
                response.status_code,
                response.text[:200],
            )
            return False

        logger.debug("exported trace %s (%d span(s))", run.trace_id, len(run.spans) + 1)
        return True
