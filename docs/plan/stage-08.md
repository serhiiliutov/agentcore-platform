# Stage 8 — The agent loop and the structural approval gate

* **Status:** complete
* **Started:** 2026-10-13
* **Estimate:** agent 8–10 h · human 3 days
* **Depends on:** Stage 2 (approvals schema), Stage 4 (the ledger), Stage 5 (the server), Stage 7 (providers)
* **Branch:** `main`

## Goal

Turn a model reply into a tool call, and make the difference between a **read that runs** and a
**write that must be approved** a property of the code rather than a promise. This is the
requirement the assignment is scored on.

## Deliverables

| Module | Responsibility |
|---|---|
| `db/repositories/approvals.py` | Propose, decide, claim, finish — with the transition guard |
| `tools/registry.py` | Discovered tools, plus whether each needs approval |
| `agent/loop.py` | One turn: chat, dispatch, feed back, stop on a proposal or the budget |
| `agent/execution.py` | **The only code that can perform a write** |
| `agent/handler.py` | Translation to the AgentCore contract + the per-invocation factory |
| `agent/gate.py` | The adapter: decide, then execute only if approved |
| `api/approval_routes.py` | `POST /invocations/approvals/{id}/decide`, injectable like `/invocations` |
| `tests/unit/test_approval_gate.py` | 22 tests proving the gate |
| `tests/unit/test_decision_route.py` | 13 tests over the route and the adapter's seam |

## The four claims, and where each is enforced

| Claim | Enforced by |
|---|---|
| A requested write is not performed | `AgentLoop._propose_if_write` records a row; the loop has no write call |
| Execution needs approval | `ApprovalRepository.claim_for_execution` refuses anything not `approved` |
| What runs is what was approved | `WriteExecutor._arguments` sends `approval.payload` |
| One approval, one ticket | `client_request_id` minted at *proposal* time, unique on the row |

## The join between Stage 4 and Stage 8

The single most important line in this stage is that the idempotency key is generated in
`ApprovalRepository.propose`, **not** in the executor:

```python
client_request_id = client_request_id or f"appr-{uuid.uuid4().hex}"
```

Because the key exists before anyone approves anything, a crash between approval and execution
reuses it and Stage 4's ledger answers `replayed` instead of creating a second ticket. A key
minted at execution time would be new on every attempt, and **every single-execution test would
still pass** — which is why one test asserts the key exists on the row before a decision is
made.

## Definition of done

| Check | Command | Result |
|---|---|---|
| Lint is clean | `python -m devtasks lint` | **pass** |
| Formatting is clean | `python -m devtasks fmt` | **pass** |
| Types are clean | `python -m devtasks typecheck` | **pass** — `66 source files` |
| All tests | `python -m devtasks test-unit` | **pass** — `232 passed` |
| A proposed write never reaches the transport | `test_a_write_the_model_requests_is_not_called` | **pass** |
| Nothing but an approved row can execute | `test_a_pending_write_cannot_be_executed` | **pass** |
| The stored payload is what is sent | `test_execution_sends_the_stored_payload` | **pass** |
| The row's key wins over the model's | `test_execution_sends_the_rows_key_not_the_payloads` | **pass** |
| A second execution is refused | `test_executing_twice_does_not_call_the_tool_twice` | **pass** |
| A replay does not claim a new ticket | `test_a_replay_is_reported_without_claiming_a_new_ticket` | **pass** |
| A refusal is a state, not a traceback | `test_a_failing_write_is_recorded_not_raised` | **pass** |
| Declining does not reach the transport | `test_declining_does_not_execute` | **pass** |
| An unwired decision route says so | `test_the_route_exists_even_with_no_handler` | **pass** |
| A blocked decision is a 409, not a 400 | `test_a_conflict_is_a_409_rather_than_a_400` | **pass** |
| The whole path: propose, approve, execute | `test_execution_sends_the_stored_payload_through_the_gate` | **pass** |

## Findings worth recording

* **The suite found a real schema bug, latent since Stage 2.** `IdempotencyKey` declared
  `expires_at` with `index=True` *and* an explicit `Index` bearing the same name the naming
  convention derives from the column. The metadata held two indexes under one name and
  `create_all` failed on the second. Nothing had ever called `create_all` on this metadata —
  Stage 2 tested *migrations*, and Alembic emitted a single index — so the duplicate was
  invisible until a test needed the schema built from the models. The reasoning is recorded on
  the column, because the next person to add an index will be looking there.
* **In-memory SQLite needs `StaticPool`.** The default pool hands out a new connection per
  checkout, so `create_all` ran against one connection and every later query against a
  different, empty database. A fixture property, so the fix is in the fixture.
* **A test caught the `Approval | None` return being ignored.** Four call sites read `.state`
  off a possibly-`None` row. It passes at runtime because the row exists; `mypy` was right that
  the optional is doing real work, and a helper now asserts it.
* **The loop's bare `except` and the executor's bare `except` do opposite things, correctly.**
  In the loop a transport failure is *swallowed* — nothing needs recording, and a traceback
  would leave the model nothing to act on. In the executor the same class of failure is
  *recorded*, because the row would otherwise stay `approved` and be re-attempted on every
  sweep. Both docstrings say so, because the next reader will assume one of them is a mistake.
* **A speculative `writer` property was removed before committing.** It returned `None` and
  nothing called it; a property justified as "present on the off-chance" is how accidental API
  surface becomes load-bearing.
* **The loop proposes at most one write per turn, and ends the turn there.** Not tidiness: the
  write's result does not exist yet, so continuing would ask the model to reason about a ticket
  that has not been created, and feeding back a "pending" placeholder invites it to summarise
  an action that may be refused.

## Result

The gate is structural. There is one file that can write, it cannot be reached without an
approved row, and the row carries everything needed to execute — so "approve one thing, run
another" is impossible rather than merely discouraged. 232 tests, all four gates green.

## Carried forward

* **Stage 9 (memory) must persist the session before a write can be proposed.**
  `InvocationHandlerFactory` currently mints a session id without inserting a `ChatSession`
  row, and `approvals.session_id` is a foreign key onto `sessions.id` with
  `PRAGMA foreign_keys=ON`. So the first proposal in a fresh session would fail with a foreign
  key violation. This is found and recorded rather than left for the integration test to
  discover, and it is the first thing Stage 9 should fix.
* **Stage 11 (web UI)** needs the decision route plus a list of pending proposals. The route
  exists; the list is an endpoint the UI stage adds, and `ApprovalRepository.pending` is
  already written and tested for it.
