# Stage 10 — Observability: spans, traces, and the read side

* **Status:** complete
* **Started:** 2026-10-13
* **Estimate:** agent 6–8 h · human 2 days
* **Depends on:** Stage 2 (the `traces` table), Stage 8 (the loop and the handler)
* **Branch:** `main`

## Goal

Make any single run explainable afterwards: what it did, in what order, how long each part took,
and whether it worked — answerable months later, from a machine that did not run it.

## Deliverables

| Module | Responsibility |
|---|---|
| `observability/spans.py` | `Span` — one unit of work, serialisable to one JSON line |
| `observability/tracer.py` | Collects a run's spans and writes them; must never fail the run |
| `db/repositories/traces.py` | Stores a run's summary; every read scoped by user |
| `agent/handler.py` | Traces every invocation — the wiring that makes the above reachable |
| `tests/unit/test_observability.py` | 24 tests |

## The split the schema already fixed

Spans go to a JSONL file under `TRACE_DIR`; one summary row goes to `traces`. The schema's own
docstring explains why, and this stage implements it rather than revisiting it: a trace is 20–200
events, a few holding tool payloads, and storing them as rows would mean either a loose JSON
column (a file with extra steps) or a span table that has to anticipate every future event type.

## Three decisions

**A tracing failure never fails an invocation.** Every write is wrapped and reported through a
callback rather than raised. The trace exists to explain a run, so refusing the run because the
explanation could not be recorded inverts the purpose — and the callback is what stops that from
being silent, since tracing quietly stopping is worse than tracing loudly failing.

**Spans are written once, at the end, into a temporary file that is then moved into place.** A
reader never opens a half-file that looks whole. A truncated trace is worse than a missing one,
because a missing trace is unambiguous while a truncated one is trusted.

**Only a genuine `error` stop marks a trace failed.** A turn that ended by proposing a write is a
success — the agent is waiting for a human — and reporting it as an error would put the approval
workflow into every failure listing. That is the same confusion the contract's `stop_reason`
exists to prevent at the HTTP layer.

## Definition of done

| Check | Command | Result |
|---|---|---|
| Formatting is clean | `python -m devtasks fmt` | **pass** |
| Lint is clean | `python -m devtasks lint` | **pass** |
| Types are clean | `python -m devtasks typecheck` | **pass** — `77 source files` |
| All tests | `python -m devtasks test-unit` | **pass** — `281 passed` |
| An unwritable trace is reported, not raised | `test_an_unwritable_directory_is_reported_not_raised` | **pass** |
| A crashed run is traced as an error | `test_a_failed_trace_is_recorded_as_an_error` | **pass** |
| No partial trace is left behind | `test_no_partial_file_is_left_behind` | **pass** |
| A trace is readable only by its owner | `test_a_trace_is_readable_only_by_its_owner` | **pass** |
| An unknown status is refused | `test_an_unknown_status_is_refused` | **pass** |

## Findings worth recording

* **The wiring was a separate commit from both halves, deliberately.** `Tracer` and
  `TraceRepository` each passed their own tests while nothing imported either — the integration
  gap that is easiest to miss precisely because every unit test is green. Tracing is now on the
  invocation path, which is what makes the stage real rather than a library.
* **The trace carries `prompt_chars`, not the prompt.** The traces table is scoped by user
  because prompts are sensitive; copying the text into a plain file anyone with directory access
  could read would route around that scoping. The conversation already stores the text under the
  same rule.
* **The trace summary is written after the turn is stored**, so a row never describes a turn the
  conversation does not contain. If appending the turn raised there would be no trace either — and
  of the two artefacts, a trace of an unsaved turn is the more misleading.
* **`Span.duration_ms` is `None` while open, `0` when closed.** Zero is a real duration; an
  unfinished span does not have one. `add()` sets `ended_at` immediately, so an instant event is
  distinguishable from a span that never finished.
* **`Span.as_dict` always emits every key**, including the nulls, so a reader never has to handle
  "missing" differently from "null". The first trace anyone opens is usually an interrupted one.
* **`provider` is recorded as the class name**, not a configured string, so it cannot drift from
  the object that actually did the work.

## Result

Any run is now findable and explainable: a row answers "which run, how long, how many calls, did
it fail", and the JSONL beside it answers "what happened, in what order". 281 tests, all four
gates green.

## Carried forward

* **Nothing reads a trace file yet.** The repository exposes the row and its path; opening and
  reading the spans is a job for the API and the UI in Stage 11. Recorded so the absence is known
  rather than assumed to be handled.
* **`DEFAULT_HISTORY_LIMIT` is still a turn count, not a token budget** — carried from Stage 9.
  The token counts a fix would base itself on are now recorded and visible, so the work is
  unblocked rather than merely noted.
* **No retention policy for trace files.** They accumulate under `TRACE_DIR` and nothing prunes
  them. A real deployment needs one; it belongs with Stage 12's hardening or with the deployment
  work in Stage 14.
