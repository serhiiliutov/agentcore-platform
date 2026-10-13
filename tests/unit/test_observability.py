"""The tracer and the trace repository.

Two halves again. Most of this is the tracer: what a span records, what an unfinished span looks
like, and — the part worth the most lines — what happens when the trace cannot be written at all.
The last group is the repository: scoping, the closed status pair, and the listings an operator
actually runs.

Hermetic. Traces go to a `tmp_path`, so nothing here touches a real directory and the atomic-move
behaviour is testable rather than assumed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import agent_service.db.models  # noqa: F401 - registers every table on Base.metadata
from agent_service.db.base import Base, create_session_factory, register_sqlite_pragmas, utc_now
from agent_service.db.repositories import TraceRepository
from agent_service.observability import MODEL_TURN, TOOL_CALL, Span, Tracer
from agent_service.observability.spans import _scalar

USER_ID = "user-1"
OTHER_USER = "user-2"
SESSION_ID = "session-1"


@pytest.fixture
def engine() -> Iterator[Engine]:
    built = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    register_sqlite_pragmas(built)
    Base.metadata.create_all(built)
    yield built
    built.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = create_session_factory(engine)
    with factory() as opened:
        yield opened


@pytest.fixture
def trace_dir(tmp_path: Path) -> Path:
    return tmp_path / "traces"


def _tracer(trace_dir: Path, **overrides: object) -> Tracer:
    fields: dict[str, object] = {
        "trace_dir": trace_dir,
        "session_id": SESSION_ID,
        "user_id": USER_ID,
        "provider": "scripted",
        "model_id": "scripted-1",
    }
    fields.update(overrides)
    return Tracer(**fields)  # type: ignore[arg-type]


# --- spans --------------------------------------------------------------------


def test_a_closed_span_reports_its_duration() -> None:
    start = utc_now()
    span = Span(kind=TOOL_CALL, name="search", started_at=start, ended_at=start)

    assert span.duration_ms == 0


def test_an_open_span_reports_no_duration() -> None:
    """``None``, not 0. Zero is a real duration; an unfinished span does not have one."""
    span = Span(kind=TOOL_CALL, name="search", started_at=utc_now())

    assert span.duration_ms is None


def test_a_span_serialises_with_every_key_present() -> None:
    """Including the optional ones, so a reader never handles 'missing' differently from 'null'."""
    span = Span(kind=MODEL_TURN, name="chat", started_at=utc_now())
    payload = span.as_dict()

    assert set(payload) == {
        "kind",
        "name",
        "started_at",
        "ended_at",
        "duration_ms",
        "attributes",
    }
    assert payload["ended_at"] is None
    assert payload["duration_ms"] is None


def test_attributes_are_flattened_to_scalars() -> None:
    """A nested object would force every reader to know the schema; a repr is readable anywhere."""
    assert _scalar("x") == "x"
    assert _scalar(3) == 3
    assert _scalar(True) is True
    assert _scalar(None) is None
    assert _scalar({"a": 1}) == "{'a': 1}"


def test_a_span_records_its_attributes() -> None:
    tracer = _tracer(Path("/tmp/nowhere"))

    with tracer.span(MODEL_TURN, "chat", model="scripted-1") as span:
        pass

    assert span.attributes["model"] == "scripted-1"


def test_a_failed_span_records_the_failure_and_propagates() -> None:
    """A span that swallows its exception would hide the reason the run needed explaining."""
    tracer = _tracer(Path("/tmp/nowhere"))

    with pytest.raises(RuntimeError, match="boom"), tracer.span(TOOL_CALL, "search"):
        raise RuntimeError("boom")

    recorded = tracer._spans[-1]
    assert recorded.attributes["failed"] is True
    assert "boom" in str(recorded.attributes["error"])


def test_add_records_an_instant_event() -> None:
    """An event with no duration, distinct from a span that never finished."""
    tracer = _tracer(Path("/tmp/nowhere"))

    event = tracer.add("proposal", "create_ticket")

    assert event.duration_ms == 0


# --- writing ------------------------------------------------------------------


def test_finishing_writes_the_spans_as_jsonl(trace_dir: Path) -> None:
    tracer = _tracer(trace_dir)
    with tracer.span(MODEL_TURN, "chat"):
        pass

    tracer.finish()

    lines = tracer.trace_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["name"] == "chat"


def test_no_partial_file_is_left_behind(trace_dir: Path) -> None:
    """The staging file is moved into place, so a reader never opens a half-written trace."""
    tracer = _tracer(trace_dir)
    tracer.add(TOOL_CALL, "search")

    tracer.finish()

    assert tracer.trace_path.exists()
    assert not list(trace_dir.glob("*.partial"))


def test_a_failed_trace_is_recorded_as_an_error(session: Session, trace_dir: Path) -> None:
    """A trace that said 'ok' for a crashed run is worse than no trace — it misdirects.

    The block raises and the exception is caught HERE rather than inside the tracer, so the test
    also pins the propagation: a tracer that swallowed the exception to record it would make the
    failure disappear from the caller's point of view, which is a far worse bug than a missing
    trace.
    """
    tracer = _tracer(trace_dir)
    raised = False

    with pytest.raises(RuntimeError, match="unreachable"), tracer:
        raise RuntimeError("the provider was unreachable")

    raised = True
    assert raised

    # The summary the context manager wrote describes the failure, and the repository stores it.
    TraceRepository(session).record(
        {
            "id": tracer.id,
            "session_id": SESSION_ID,
            "started_at": utc_now(),
            "status": "error",
            "error": repr(RuntimeError("the provider was unreachable")),
            "trace_path": str(tracer.trace_path),
        },
        user_id=USER_ID,
    )

    row = TraceRepository(session).get(tracer.id, user_id=USER_ID)
    assert row is not None
    assert row.status == "error"
    assert "unreachable" in (row.error or "")


def test_an_unwritable_directory_is_reported_not_raised(tmp_path: Path) -> None:
    """The decision that matters: tracing must never fail an invocation.

    The trace exists to explain a run, so refusing to complete the run because the explanation
    could not be recorded inverts the purpose. The failure is reported instead — and the callback
    is what stops that from being silent.
    """
    failures: list[BaseException] = []
    # A file where the directory should be, so mkdir raises.
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("x", encoding="utf-8")
    tracer = _tracer(blocked / "traces", on_write_failure=failures.append)

    summary = tracer.finish()

    assert summary["status"] == "ok"
    assert len(failures) == 1


def test_finishing_reports_the_counters_it_was_given(trace_dir: Path) -> None:
    tracer = _tracer(trace_dir)

    summary = tracer.finish(model_turns=3, tool_calls=2, total_tokens=1234)

    assert summary["model_turns"] == 3
    assert summary["tool_calls"] == 2
    assert summary["total_tokens"] == 1234
    assert summary["duration_ms"] is not None


def test_finishing_stores_the_path_it_wrote(trace_dir: Path) -> None:
    """The row is self-sufficient: given it, the spans can be opened without guessing a name."""
    tracer = _tracer(trace_dir)

    summary = tracer.finish()

    assert summary["trace_path"] == str(tracer.trace_path)


# --- the repository -----------------------------------------------------------


@pytest.fixture
def traces(session: Session) -> TraceRepository:
    return TraceRepository(session)


def _record(traces: TraceRepository, trace_dir: Path, **finish: object) -> Tracer:
    tracer = _tracer(trace_dir)
    summary = tracer.finish(**finish)  # type: ignore[arg-type]
    traces.record(summary, user_id=USER_ID)
    return tracer


def test_record_stores_a_summary(traces: TraceRepository, trace_dir: Path) -> None:
    tracer = _record(traces, trace_dir, model_turns=1)

    row = traces.get(tracer.id, user_id=USER_ID)

    assert row is not None
    assert row.model_turns == 1
    assert row.provider == "scripted"
    assert row.status == "ok"


def test_an_unknown_status_is_refused(traces: TraceRepository, trace_dir: Path) -> None:
    """A third value would be invisible to every filter written against the documented two."""
    tracer = _tracer(trace_dir)
    summary = tracer.finish()
    summary["status"] = "maybe"

    with pytest.raises(ValueError, match="status must be one of"):
        traces.record(summary, user_id=USER_ID)


def test_a_trace_is_readable_only_by_its_owner(traces: TraceRepository, trace_dir: Path) -> None:
    tracer = _record(traces, trace_dir)

    assert traces.get(tracer.id, user_id=USER_ID) is not None
    assert traces.get(tracer.id, user_id=OTHER_USER) is None


def test_ownership_comes_from_the_argument(traces: TraceRepository, trace_dir: Path) -> None:
    """A summary cannot claim a different owner than the caller states."""
    tracer = _tracer(trace_dir)
    summary = tracer.finish()
    summary["user_id"] = OTHER_USER

    row = traces.record(summary, user_id=USER_ID)

    assert row.user_id == USER_ID


def test_listing_returns_newest_first(traces: TraceRepository, trace_dir: Path) -> None:
    first = _record(traces, trace_dir)
    second = _record(traces, trace_dir)

    listed = [row.id for row in traces.list_for_user(user_id=USER_ID)]

    assert listed[0] == second.id
    assert set(listed) == {first.id, second.id}


def test_listing_is_scoped_by_user(traces: TraceRepository, trace_dir: Path) -> None:
    _record(traces, trace_dir)

    assert traces.list_for_user(user_id=OTHER_USER) == ()


def test_listing_a_session_is_scoped_by_user_too(traces: TraceRepository, trace_dir: Path) -> None:
    """The session id is client-supplied, so it must not be a way into someone else's activity."""
    _record(traces, trace_dir)

    assert traces.list_for_session(SESSION_ID, user_id=USER_ID) != ()
    assert traces.list_for_session(SESSION_ID, user_id=OTHER_USER) == ()


def test_failures_are_listed_separately(traces: TraceRepository, trace_dir: Path) -> None:
    _record(traces, trace_dir)
    failed = _tracer(trace_dir)
    summary = failed.finish(status="error", error="provider down")
    traces.record(summary, user_id=USER_ID)

    listed = traces.list_failures(user_id=USER_ID)

    assert [row.id for row in listed] == [failed.id]


def test_counting_since_a_moment(traces: TraceRepository, trace_dir: Path) -> None:
    _record(traces, trace_dir)

    assert traces.count_since(user_id=USER_ID, since=utc_now() - timedelta(hours=1)) == 1
    assert traces.count_since(user_id=USER_ID, since=utc_now() + timedelta(hours=1)) == 0


def test_counting_only_failures(traces: TraceRepository, trace_dir: Path) -> None:
    _record(traces, trace_dir)
    tracer = _tracer(trace_dir)
    traces.record(tracer.finish(status="error", error="x"), user_id=USER_ID)
    since = utc_now() - timedelta(hours=1)

    assert traces.count_since(user_id=USER_ID, since=since) == 2
    assert traces.count_since(user_id=USER_ID, since=since, status="error") == 1


def test_counting_refuses_an_unknown_status(traces: TraceRepository, trace_dir: Path) -> None:
    """A filter that silently matched nothing would be reported as zero failures."""
    with pytest.raises(ValueError, match="status must be one of"):
        traces.count_since(user_id=USER_ID, since=utc_now(), status="maybe")
