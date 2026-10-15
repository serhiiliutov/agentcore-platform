"""Collects one invocation's spans and writes them where they can be read later.

The design the schema already fixed: spans go to a JSONL file under ``TRACE_DIR``, and one row
goes into ``traces`` as the searchable summary. This module is the writer for both.

Three decisions, and the first is the one that matters.

**A tracing failure must never fail an invocation.** Every write here is wrapped, and a failure
is reported through a callback rather than raised. The reasoning is not defensive habit: the
trace exists to explain a run, so refusing to complete the run because the explanation could not
be recorded inverts the purpose. The callback exists so the failure is *visible* — swallowing it
silently would mean tracing quietly stopped working and nobody noticed until an incident when the
traces were needed.

**Spans are written once, at the end, not appended as they happen.** A crash mid-turn then leaves
no partial file to mislead a reader into thinking a run completed — and a file that appears whole
is exactly what someone assumes when they open one at 2am. The cost is losing the spans of a run
that dies hard, which is the correct trade: a missing trace is unambiguous, a truncated one is
not.

**The summary row is written even when the spans are not.** If the file write fails, the row still
records that an invocation happened, how long it took and how it ended. A row whose ``trace_path``
points at nothing is a worse outcome than no row only if the path were trusted blindly — and the
column is documented as a location, not a guarantee.

``trace_path`` is derived from the trace id, and stored anyway. Derived alone would couple every
reader to this file's naming convention; stored alone would be lost when the row is written before
the file. It is both, so a row is self-sufficient and the writer does not have to know where it
will put things before it decides.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from agent_service.db.base import utc_now

from .otlp import RunExport
from .spans import Span

logger = logging.getLogger(__name__)

#: Called when a trace could not be written. Given the exception, so a caller can count or alert.
WriteFailureCallback = Callable[[BaseException], None]


class Tracer:
    """One invocation's spans, and the summary row they belong to.

    Used as a context manager, so ``finish`` runs even when the block raises — a trace of a failed
    turn is more valuable than a trace of a successful one, and the naive version that only writes
    on the success path loses precisely the runs anyone wants to read.
    """

    def __init__(
        self,
        *,
        trace_dir: Path,
        session_id: str,
        user_id: str,
        provider: str = "",
        model_id: str = "",
        trace_id: str | None = None,
        on_write_failure: WriteFailureCallback | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.id = trace_id or str(uuid.uuid4())
        self._trace_dir = trace_dir
        self._session_id = session_id
        self._user_id = user_id
        self._provider = provider
        self._model_id = model_id
        self._on_write_failure = on_write_failure
        self._clock = clock

        self._spans: list[Span] = []
        self._started_at = clock()
        self._ended_at: datetime | None = None
        self._status = "ok"
        self._error: str | None = None

    @property
    def trace_path(self) -> Path:
        """Where this run's spans will be written.

        ``<id>.jsonl`` in the trace directory. Flat rather than date-partitioned: a run is found
        by its id from the row, and a directory tree would only add a lookup that the row already
        answers.
        """
        return self._trace_dir / f"{self.id}.jsonl"

    # --- recording ------------------------------------------------------------

    @contextmanager
    def span(self, kind: str, name: str, **attributes: Any) -> Iterator[Span]:
        """Record one unit of work.

        The span is closed in a ``finally``, so an exception inside the block still produces a
        span with an end time and a ``failed`` attribute. Without that, a turn that raised would
        be a turn with no record of what it was doing when it raised — the one case where the
        sequence of events is the whole question.
        """
        opened = Span(kind=kind, name=name, started_at=self._clock(), attributes=dict(attributes))
        self._spans.append(opened)
        try:
            yield opened
        except BaseException as exc:
            opened.attributes.setdefault("failed", True)
            opened.attributes.setdefault("error", repr(exc))
            raise
        finally:
            opened.ended_at = self._clock()

    def add(self, kind: str, name: str, **attributes: Any) -> Span:
        """Record an event with no duration — a decision, a proposal, a state change.

        A span with ``ended_at`` set immediately, so ``duration_ms`` is genuinely 0 rather than
        absent: the event happened at an instant, and that is a different thing from an unfinished
        span. Keeping the distinction means a reader can tell "took no time" from "never finished".
        """
        moment = self._clock()
        event = Span(
            kind=kind, name=name, started_at=moment, ended_at=moment, attributes=dict(attributes)
        )
        self._spans.append(event)
        return event

    def fail(self, error: str) -> None:
        """Mark the run as failed, without ending it.

        Separate from ``finish`` because the loop may recover from something the caller still
        wants recorded — a tool failure that was fed back to the model is a failure of a span, not
        of the run.
        """
        self._status = "error"
        self._error = error

    # --- finishing ------------------------------------------------------------

    @property
    def duration_ms(self) -> int | None:
        if self._ended_at is None:
            return None
        return int((self._ended_at - self._started_at).total_seconds() * 1000)

    def finish(
        self,
        *,
        status: str = "ok",
        error: str | None = None,
        model_turns: int = 0,
        tool_calls: int = 0,
        total_tokens: int = 0,
    ) -> dict[str, Any]:
        """Close the run and write both artefacts. Returns the summary it wrote.

        Returns the values rather than only persisting them so a caller — or a test — can assert
        on what was recorded without reading the database back. The row is written by the caller's
        repository, not here: this module knows how to describe a run, and the repository knows how
        to store one, and neither should learn the other's job.
        """
        self._ended_at = self._clock()
        if status != "ok" or error is not None:
            self._status = status
            self._error = error or self._error

        self._write_spans()
        return {
            "id": self.id,
            "session_id": self._session_id,
            "user_id": self._user_id,
            "started_at": self._started_at,
            "ended_at": self._ended_at,
            "duration_ms": self.duration_ms,
            "status": self._status,
            "provider": self._provider,
            "model_id": self._model_id,
            "model_turns": model_turns,
            "tool_calls": tool_calls,
            "total_tokens": total_tokens,
            "error": self._error,
            "trace_path": str(self.trace_path),
        }

    def export_record(self) -> RunExport:
        """This run as the values an exporter needs.

        Only valid once `finish` has run: before that there is no end time and no final status, and
        exporting half a description of a run is worse than exporting nothing — a collector would
        show a trace that looks complete and stops early.

        This module deliberately does not know what an exporter *is*. It produces the record and the
        caller hands it on; putting an HTTP client in the module whose job is writing files would
        make every test of the writer need a transport.
        """
        if self._ended_at is None:
            raise RuntimeError("export_record() needs a finished run; call finish() first")

        return RunExport(
            trace_id=self.id,
            session_id=self._session_id,
            user_id=self._user_id,
            provider=self._provider,
            model_id=self._model_id,
            status=self._status,
            error=self._error,
            started_at=self._started_at,
            ended_at=self._ended_at,
            spans=tuple(self._spans),
        )

    def _write_spans(self) -> None:
        """Write the spans as JSONL, reporting rather than raising on failure.

        Written to a temporary file and then moved into place, so a reader never sees a half-file
        that looks like a complete trace. ``os.replace`` is atomic within a filesystem, which is
        what makes the reader's assumption safe rather than merely usually true.
        """
        try:
            self._trace_dir.mkdir(parents=True, exist_ok=True)
            staging = self.trace_path.with_suffix(".jsonl.partial")
            with staging.open("w", encoding="utf-8") as handle:
                for span in self._spans:
                    handle.write(json.dumps(span.as_dict(), default=str) + "\n")
            staging.replace(self.trace_path)
        except OSError as exc:
            self._report(exc)
        except (TypeError, ValueError) as exc:
            # A span that will not serialise is a bug in an attribute, not an outage. Reported the
            # same way: the run continues and the problem is visible.
            self._report(exc)

    def _report(self, exc: BaseException) -> None:
        """Tell someone, without failing the run.

        Logged always, and passed to the callback when one was supplied. Both, rather than either:
        the log is the record, and the callback is what lets a caller count failures or alert —
        and a caller that supplies neither still gets the log.
        """
        logger.warning("could not write trace %s: %s", self.id, exc)
        if self._on_write_failure is not None:
            self._on_write_failure(exc)

    # --- context manager ------------------------------------------------------

    def __enter__(self) -> Tracer:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        """Record how the block ended, and write the spans.

        A block that raised is recorded as an error trace with the exception's own text, because
        the alternative — a trace that says ``ok`` for a run that crashed — is worse than having no
        trace at all: it would send a reader looking in the wrong direction.
        """
        if exc is not None:
            self.finish(status="error", error=repr(exc))
        else:
            self.finish()
