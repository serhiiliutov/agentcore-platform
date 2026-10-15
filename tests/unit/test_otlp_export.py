"""The OTLP mapping, asserted document by document.

The exporter exists because `OTEL_EXPORTER_OTLP_ENDPOINT` was documented and read by no code — the
same defect class as `ALLOW_RUNTIME_KEY_ENTRY`. What it must not become is a second one: a variable
that now *is* read, in a module whose output no collector can parse. Nothing about OTLP is verified
by "the code ran", so every assertion here is on the wire document itself.

Hermetic. `httpx.MockTransport` stands in for a collector, so the transport-level behaviour —
accepted, rejected, unreachable — is exercised without a socket, and `build_payload` is asserted as
a pure function. No test in this file opens a connection.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_service.observability import MODEL_TURN, TOOL_CALL, RunExport, Span, Tracer
from agent_service.observability.otlp import (
    GEN_AI_OPERATION,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_SYSTEM,
    GEN_AI_TOOL_NAME,
    STATUS_ERROR,
    STATUS_OK,
    TRACES_PATH,
    OtlpExporter,
    build_payload,
)

SERVICE = "agentcore-platform"
STARTED = datetime(2026, 10, 15, 9, 0, 0, tzinfo=UTC)


def _span(kind: str, name: str, *, offset_s: float = 0.0, **attributes: object) -> Span:
    began = STARTED + timedelta(seconds=offset_s)
    return Span(
        kind=kind,
        name=name,
        started_at=began,
        ended_at=began + timedelta(milliseconds=250),
        attributes=dict(attributes),
    )


def _run(*spans: Span, status: str = "ok", error: str | None = None) -> RunExport:
    return RunExport(
        trace_id="3f0c4e2a-1d6b-4c8e-9f2a-5b7d8e1a2c34",
        session_id="sess-abc",
        user_id="dana",
        provider="openai",
        model_id="gpt-4o",
        status=status,
        error=error,
        started_at=STARTED,
        ended_at=STARTED + timedelta(seconds=3),
        spans=spans,
    )


def _spans_of(document: dict[str, Any]) -> list[dict[str, Any]]:
    # `Any` rather than a precise type, because this walks a JSON document whose shape is asserted
    # by the tests rather than by the annotations — and a `dict[str, object]` here makes every
    # index into it `object`, which turns a readable assertion into a type error at every line.
    resource_spans = document["resourceSpans"]
    assert isinstance(resource_spans, list)
    scope_spans = resource_spans[0]["scopeSpans"]
    assert isinstance(scope_spans, list)
    spans = scope_spans[0]["spans"]
    assert isinstance(spans, list)
    return spans


def _attribute_value(span: dict[str, Any], key: str) -> Any:
    attributes = span["attributes"]
    assert isinstance(attributes, list)
    for attribute in attributes:
        if attribute["key"] == key:
            value = attribute["value"]
            # One of the four typed wrappers, and the tests below unwrap them explicitly where the
            # distinction matters. Here the wrapper is only peeled so a caller can compare values.
            assert isinstance(value, dict) and len(value) == 1
            return next(iter(value.values()))
    raise AssertionError(f"{key} is missing; span has {[a['key'] for a in attributes]}")


# --- the document's shape -----------------------------------------------------


def test_one_resource_and_one_scope_wrap_every_span() -> None:
    """The collector groups by resource, so one run must describe one service, once."""
    document = build_payload(_run(_span(TOOL_CALL, "search_tickets")), service_name=SERVICE)

    resource = document["resourceSpans"][0]["resource"]
    assert _attribute_value(resource, "service.name") == SERVICE
    assert len(document["resourceSpans"]) == 1
    assert len(document["resourceSpans"][0]["scopeSpans"]) == 1


def test_the_run_becomes_a_root_span_with_the_calls_under_it() -> None:
    """A run is one operation, so it is one trace — not twenty unrelated spans.

    Without the root, a collector shows a flat list of model turns and tool calls with nothing
    saying which invocation they belonged to.
    """
    run = _run(_span(MODEL_TURN, "invocation"), _span(TOOL_CALL, "search_tickets"))

    spans = _spans_of(build_payload(run, service_name=SERVICE))

    assert len(spans) == 3, "one root plus one per recorded span"
    root = spans[0]
    assert root["name"] == "invoke_agent"
    assert "parentSpanId" not in root or root["parentSpanId"] in ("", None)
    for child in spans[1:]:
        assert child["parentSpanId"] == root["spanId"]


def test_the_identity_of_the_run_lands_on_the_root_span() -> None:
    """Session, user, provider and model are what make a trace searchable in a collector."""
    root = _spans_of(build_payload(_run(), service_name=SERVICE))[0]

    assert _attribute_value(root, "session.id") == "sess-abc"
    assert _attribute_value(root, "enduser.id") == "dana"
    assert _attribute_value(root, GEN_AI_SYSTEM) == "openai"
    assert _attribute_value(root, GEN_AI_REQUEST_MODEL) == "gpt-4o"


def test_ids_are_hex_of_the_lengths_the_protocol_requires() -> None:
    """A collector rejects a trace id that is not 32 hex characters, and silently-ish."""
    spans = _spans_of(build_payload(_run(_span(TOOL_CALL, "search_tickets")), service_name=SERVICE))

    for span in spans:
        assert len(span["traceId"]) == 32, span["traceId"]
        assert int(span["traceId"], 16) >= 0, "must be hex"
        assert len(span["spanId"]) == 16, span["spanId"]

    ids = [span["spanId"] for span in spans]
    assert len(set(ids)) == len(ids), "two spans in one trace must not share an id"


def test_the_same_run_produces_the_same_ids_twice() -> None:
    """Derived rather than random, so re-exporting a run is not two runs to the collector."""
    run = _run(_span(TOOL_CALL, "search_tickets"))

    first = _spans_of(build_payload(run, service_name=SERVICE))
    second = _spans_of(build_payload(run, service_name=SERVICE))

    assert first == second


def test_two_same_named_spans_in_one_run_stay_distinct() -> None:
    """A turn that searches twice must not report one span id twice."""
    run = _run(_span(TOOL_CALL, "search_tickets"), _span(TOOL_CALL, "search_tickets"))

    ids = [span["spanId"] for span in _spans_of(build_payload(run, service_name=SERVICE))]

    assert len(set(ids)) == 3


# --- timestamps ---------------------------------------------------------------


def test_timestamps_are_nanoseconds_since_the_epoch_as_strings() -> None:
    """OTLP encodes 64-bit integers as strings, and a JSON number would lose precision.

    Asserted on an exact value rather than on a pattern: a timestamp that is merely
    string-shaped but computed wrong — the classic `timestamp() * 1e9` float rounding — passes a
    type check and puts spans in the wrong second.
    """
    run = _run(_span(TOOL_CALL, "search_tickets", offset_s=1.5), status="ok")

    root = _spans_of(build_payload(run, service_name=SERVICE))[0]

    expected_root = int((STARTED - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds() * 10**9)
    assert root["startTimeUnixNano"] == str(expected_root)
    assert isinstance(root["startTimeUnixNano"], str)

    child = _spans_of(build_payload(run, service_name=SERVICE))[1]
    assert child["startTimeUnixNano"] == str(expected_root + 1_500_000_000)
    assert child["endTimeUnixNano"] == str(expected_root + 1_750_000_000)


def test_an_unfinished_span_ends_with_the_run() -> None:
    """The process died mid-turn; the span must still be exported rather than dropped.

    Dropping it would lose the record of what the turn was doing when it stopped, which is the one
    thing anyone wants from a crash.
    """
    unfinished = Span(
        kind=TOOL_CALL, name="search_tickets", started_at=STARTED, ended_at=None, attributes={}
    )

    span = _spans_of(build_payload(_run(unfinished), service_name=SERVICE))[1]

    assert span["endTimeUnixNano"] == str(
        int((STARTED + timedelta(seconds=3) - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds() * 10**9)
    )


# --- the GenAI conventions ----------------------------------------------------


def test_a_model_turn_is_named_as_a_chat_operation() -> None:
    """The convention's name is what makes a collector render a model call as one."""
    span = _spans_of(build_payload(_run(_span(MODEL_TURN, "invocation")), service_name=SERVICE))[1]

    assert span["name"] == "chat"
    assert _attribute_value(span, GEN_AI_OPERATION) == "chat"
    assert _attribute_value(span, GEN_AI_SYSTEM) == "openai"


def test_a_tool_call_carries_the_tool_name() -> None:
    """`execute_tool search_tickets` is legible in a trace list; "tool_call" repeated is not."""
    span = _spans_of(
        build_payload(_run(_span(TOOL_CALL, "search_tickets")), service_name=SERVICE)
    )[1]

    assert span["name"] == "execute_tool search_tickets"
    assert _attribute_value(span, GEN_AI_TOOL_NAME) == "search_tickets"


def test_a_span_of_our_own_vocabulary_keeps_its_kind() -> None:
    """`proposal` and `memory` are this project's words, so they are reported as such.

    Inventing a GenAI operation name for them would claim a convention that does not exist.
    """
    span = _spans_of(build_payload(_run(_span("proposal", "create_ticket")), service_name=SERVICE))[
        1
    ]

    assert span["name"] == "proposal create_ticket"
    assert _attribute_value(span, GEN_AI_OPERATION) == "proposal"
    assert _attribute_value(span, "agentcore.span.kind") == "proposal"


# --- attribute typing ---------------------------------------------------------


def test_attribute_values_are_typed_as_otlp_types_them() -> None:
    """Booleans before integers, and integers as strings.

    `bool` is a subclass of `int` in Python, so a naive check sends `True` as `1` — and a collector
    reading a flag as a number is a small lie in a place nobody looks.
    """
    run = _run(_span(TOOL_CALL, "search_tickets", count=3, cached=True, ratio=0.5, note="x"))
    span = _spans_of(build_payload(run, service_name=SERVICE))[1]

    attributes = {a["key"]: a["value"] for a in span["attributes"]}
    assert attributes["count"] == {"intValue": "3"}
    assert attributes["cached"] == {"boolValue": True}
    assert attributes["ratio"] == {"doubleValue": 0.5}
    assert attributes["note"] == {"stringValue": "x"}


def test_attributes_are_ordered_so_two_exports_are_comparable() -> None:
    """Sorted rather than insertion-ordered, so a diff of two documents is meaningful."""
    run = _run(_span(TOOL_CALL, "search_tickets", zebra=1, alpha=2))

    span = _spans_of(build_payload(run, service_name=SERVICE))[1]

    keys = [a["key"] for a in span["attributes"]]
    assert keys == sorted(keys)


# --- status -------------------------------------------------------------------


def test_a_healthy_run_is_reported_as_ok() -> None:
    spans = _spans_of(build_payload(_run(_span(TOOL_CALL, "search_tickets")), service_name=SERVICE))

    assert all(span["status"] == {"code": STATUS_OK} for span in spans)


def test_a_failed_run_marks_the_root_with_the_reason() -> None:
    """The message travels on the status, where a trace viewer shows it without expanding."""
    run = _run(status="error", error="the provider refused the turn")

    root = _spans_of(build_payload(run, service_name=SERVICE))[0]

    assert root["status"]["code"] == STATUS_ERROR
    assert root["status"]["message"] == "the provider refused the turn"


def test_a_failed_span_marks_only_that_span() -> None:
    """The run succeeded and one call inside it did not — a distinction a flat status loses."""
    run = _run(
        _span(TOOL_CALL, "search_tickets", failed=True, error="connection reset"),
        _span(TOOL_CALL, "get_ticket"),
    )

    spans = _spans_of(build_payload(run, service_name=SERVICE))

    assert spans[0]["status"] == {"code": STATUS_OK}, "the run itself did not fail"
    assert spans[1]["status"]["code"] == STATUS_ERROR
    assert spans[1]["status"]["message"] == "connection reset"
    assert spans[2]["status"] == {"code": STATUS_OK}


# --- the transport ------------------------------------------------------------


def _exporter(**kwargs: object) -> OtlpExporter:
    base: dict[str, object] = {"endpoint": "http://collector:4318", "service_name": SERVICE}
    base.update(kwargs)
    return OtlpExporter(**base)  # type: ignore[arg-type]


def test_the_endpoint_gains_the_traces_path() -> None:
    assert _exporter().traces_url == "http://collector:4318" + TRACES_PATH


def test_an_endpoint_that_already_names_the_path_is_not_doubled() -> None:
    """Both spellings are in the wild; appending blindly posts to `/v1/traces/v1/traces`."""
    exporter = _exporter(endpoint="http://collector:4318" + TRACES_PATH)

    assert exporter.traces_url == "http://collector:4318" + TRACES_PATH


def test_a_trailing_slash_does_not_produce_an_empty_segment() -> None:
    assert _exporter(endpoint="http://collector:4318/").traces_url == (
        "http://collector:4318" + TRACES_PATH
    )


async def test_an_accepted_document_is_posted_as_json_and_reported_true() -> None:
    """What the collector receives is the document, as JSON, at the traces path."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    exporter = _exporter(transport=httpx.MockTransport(handler))

    assert await exporter.export(_run(_span(TOOL_CALL, "search_tickets"))) is True

    assert len(seen) == 1
    assert seen[0].url.path == TRACES_PATH
    assert seen[0].headers["content-type"] == "application/json"
    body = json.loads(seen[0].content)
    assert body["resourceSpans"][0]["scopeSpans"][0]["spans"]


async def test_an_unreachable_collector_is_reported_and_never_raised() -> None:
    """The property the module exists around: tracing must not fail a turn.

    A refused connection, a DNS failure and a timeout are all "the trace did not leave the
    machine", and the caller is told so without an exception reaching the loop.
    """

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    exporter = _exporter(transport=httpx.MockTransport(refuse))

    assert await exporter.export(_run()) is False


async def test_a_rejected_document_is_reported_and_never_raised() -> None:
    """A 400 from a collector is the commonest OTLP integration failure, and it is not fatal."""

    def reject(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="invalid trace payload")

    exporter = _exporter(transport=httpx.MockTransport(reject))

    assert await exporter.export(_run()) is False


async def test_a_thirty_two_character_trace_id_is_not_required_to_be_a_uuid() -> None:
    """A caller that supplied its own trace id must still get a document, not an exception."""
    run = RunExport(
        trace_id="not-a-uuid-at-all",
        session_id="s",
        user_id="u",
        provider="scripted",
        model_id="",
        status="ok",
        error=None,
        started_at=STARTED,
        ended_at=STARTED,
        spans=(),
    )

    spans = _spans_of(build_payload(run, service_name=SERVICE))

    assert len(spans[0]["traceId"]) == 32


# --- the tracer's side of the seam --------------------------------------------


def test_the_tracer_hands_over_a_finished_run(tmp_path: Path) -> None:
    """The record carries the run's own values, so an exporter needs nothing else."""
    tracer = Tracer(
        trace_dir=tmp_path, session_id="sess-1", user_id="dana", provider="scripted", model_id=""
    )
    tracer.add(TOOL_CALL, "search_tickets", outcome="executed")
    tracer.finish(tool_calls=1)

    record = tracer.export_record()

    assert record.trace_id == tracer.id
    assert record.session_id == "sess-1"
    assert record.user_id == "dana"
    assert record.status == "ok"
    assert [span.name for span in record.spans] == ["search_tickets"]


def test_an_unfinished_run_cannot_be_exported(tmp_path: Path) -> None:
    """Refused rather than exported half-complete: a collector shows a trace that stops early, and
    a truncated trace reads as a finished one."""
    tracer = Tracer(trace_dir=tmp_path, session_id="s", user_id="u")

    with pytest.raises(RuntimeError, match="needs a finished run"):
        tracer.export_record()


def test_a_failed_run_carries_its_status_into_the_record(tmp_path: Path) -> None:
    """Otherwise every exported trace would be green, which is worse than exporting none."""
    tracer = Tracer(trace_dir=tmp_path, session_id="s", user_id="u")
    tracer.finish(status="error", error="the provider refused the turn")

    record = tracer.export_record()

    assert record.failed
    assert record.error == "the provider refused the turn"
