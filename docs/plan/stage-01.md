# Stage 1 — Pin the ticketing mock's contract

* **Status:** complete
* **Started:** 2026-10-13
* **Estimate:** agent 1–1.5 h · human 0.5 day
* **Depends on:** Stage 0
* **Branch:** `main` (per the working agreement: commit directly to main)

## Goal

Discover exactly how the ticketing mock behaves and write it down as executable
tests, **before** any code is built against it.

The reason this comes first: the MCP server's error taxonomy, retry policy and
idempotency ledger are all decisions about what the mock does when things go wrong.
If those decisions are made from assumptions, the retry policy will be wrong in
ways that only show up under fault injection — which is the situation the assignment
is actually testing. Twenty minutes of contract tests removes that class of guess.

The mock itself is never modified; Stage 0 pinned it with checksums for exactly this
reason.

## Deliverables

- [x] `tests/__init__.py` — the test tree is a package, so shared helpers import cleanly
- [x] `tests/contract/__init__.py`
- [x] `tests/contract/conftest.py` — in-process loader (env applied *before* import) + recorded facts
- [x] `tests/contract/test_auth_and_meta.py` — auth, health, request ids
- [x] `tests/contract/test_search.py` — filters, pagination arithmetic, ordering
- [x] `tests/contract/test_read.py` — detail view, comments, 404 body shape
- [x] `tests/contract/test_write.py` — create, comment, patch, validation, reset, **no idempotency**
- [x] `tests/contract/test_chaos.py` — `FLAKY` determinism, `Retry-After`, retry-safety proof
- [x] `tests/unit/__init__.py` — for symmetry with the contract package

## Definition of done

| Check | Command | Result |
|---|---|---|
| Lint is clean | `python -m devtasks lint` | **pass** — `All checks passed!` (6 findings fixed: 2 real, 4 typing) |
| Types are clean | `python -m devtasks typecheck` | **pass** — `Success: no issues found in 19 source files` |
| Every test passes | `python -m devtasks test-unit` | **pass** — 93 passed in 5.17s (was 25, so 68 new) |
| No Docker needed | the whole suite runs with nothing started | **pass** — the mock is loaded in-process |
| No credential needed | the whole suite runs with no API key | **pass** — the suite supplies the mock's own development key |
| The fixture is still untouched | `python -m devtasks test-unit -k integrity` | **pass** — Stage 0's checksums still match |

## What the contract tests found

These are the findings that change later stages, which is the whole point of the
exercise.

### 1. The mock has no idempotency at all

`test_the_same_payload_creates_two_distinct_tickets` proves that posting the same
ticket twice creates two tickets, with no field in either response that distinguishes
a retry from a genuine second request.

**Consequence:** the MCP server's idempotency ledger is not defensive
over-engineering, it is the only thing standing between a timeout and a duplicate
ticket. Stage 4 has its justification.

### 2. A failure is safe to retry; silence is not

The mock's middleware sleeps `LATENCY_MS`, then decides whether to inject a fault,
then routes. So:

* A **429 or 500** is raised before the route handler runs.
  `test_a_rejected_write_does_not_reach_the_handler` confirms the ticket count is
  unchanged — a create that failed that way did not create anything, so retrying it
  is safe.
* A **timeout** is a different situation. The client gave up during the sleep; the
  handler may then have run to completion. Nothing tells the caller which happened.

**Consequence:** Stage 4 needs a retry policy *and* read-back verification, and the
two cannot be collapsed into "retry with backoff". The tests state this explicitly so
the distinction survives the next person who reads the code.

### 3. There are two different error body shapes

| Producer | Statuses | Body |
|---|---|---|
| The mock's own middleware | `401`, `429`, `500` | `{"error": "...", "detail": "..."}` |
| FastAPI's own validation/routing | `400`, `404`, `422` | `{"detail": "..."}` |

**Consequence:** the error taxonomy must read both. A parser that assumes `error`
reports a missing ticket as an unexplained failure.

### 4. A `401` is never transient

Authentication is checked *before* the dice are rolled, so a bad key produces a 401
even with chaos fully enabled.

**Consequence:** `401` must never be retried, and must be surfaced to the operator as
a configuration problem rather than to the model as a transient one.

### 5. Assorted facts the tool schemas now depend on

* `GET /health` is **unprotected** — usable as a compose health check without a key.
* `page_size` is capped at **50** by the mock; a larger value is a `422`, not a
  truncation. The `search_tickets` tool must clamp rather than pass the number through.
* `status`, `priority` are **closed sets**: an unknown value is a `422`, which is
  deliberately different from "no results". The tool must keep those two apart or it
  will tell a user "nothing matched" when they actually made a typo.
* Search results **omit `comments`**; only the detail view includes them.
* Results are ordered by `updated_at` descending, and adding a comment bumps that
  timestamp — so a comment resurfaces the ticket.
* Ticket ids are **case-insensitive** on input.
* Every response, including a rejection, carries `X-Request-Id`.
* The seed is 24 tickets, `TCK-1001`…`TCK-1024`. Created ids begin at `TCK-1025`.

### 6. A small inconsistency in the mock's comment ids

Seeded comments are named `CMT-{1000 + n}-{i}`
(e.g. `CMT-1002-1`), while comments created at runtime are `CMT-{counter:04d}`
(e.g. `CMT-0001`). Two different formats for the same concept.

**Consequence:** nothing may parse a comment id to learn anything. Recorded here
because it is the kind of detail that silently breaks an eval that "verifies" an id
pattern. The mock is not being changed to fix it — it is the fixture.

## Result

68 new tests, all hermetic: no Docker, no credential, no network. The whole suite
runs in about five seconds, so it is cheap enough to sit in the default CI run rather
than being pushed behind an integration marker.

Three quality issues were caught by the gates and fixed rather than suppressed: a
useless conditional in a test (`RUF034`), a Yoda condition (`SIM300`), and three
functions returning `Any` from `response.json()` through a typed signature
(`no-any-return`). Each was fixed at the source; no `# type: ignore` remains in the
suite.

## Carried forward

* Stage 4 consumes findings 1–4 directly.
* Stage 5's `search_tickets` tool must clamp `page_size`, return `total` and
  `has_more`, and keep "invalid value" distinct from "no matches".
* Stage 13's eval cases must not assume a comment id format (finding 6).
