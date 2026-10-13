# Stage 9 — Memory: sessions, messages, facts

* **Status:** complete
* **Started:** 2026-10-13
* **Estimate:** agent 6–8 h · human 2 days
* **Depends on:** Stage 2 (schema), Stage 8 (the loop and the approval gate)
* **Branch:** `main`

## Goal

Give the agent a conversation it can continue and a person it can remember — and, first, close
the foreign-key gap Stage 8 recorded.

## Deliverables

| Module | Responsibility |
|---|---|
| `db/repositories/sessions.py` | `ensure`, `append`, `append_all`, `history`, `count` |
| `db/repositories/facts.py` | `remember`, `recall`, `recall_one`, `forget`, `all_for_user`, `purge_expired` |
| `agent/handler.py` | Now owns memory: ensures the session, loads history, stores the turn |
| `tests/unit/test_memory.py` | 25 tests |

## The carried-forward item, closed

Stage 8's `InvocationHandlerFactory` minted a session id without inserting a `ChatSession` row,
while `approvals.session_id` is a foreign key onto `sessions.id` with `PRAGMA foreign_keys=ON`.
The first write proposed in any fresh conversation would therefore have failed on a constraint.
`SessionRepository.ensure` is get-or-create and runs before anything session-scoped.

`test_a_write_proposed_in_a_fresh_session_does_not_hit_the_foreign_key` proves it through the
real `propose` path rather than by inspecting the schema.

## Where memory lives, and why

The fix was placed in `InvocationHandlerFactory.__call__` rather than given to callers. Its
`history` parameter was removed rather than filled: any caller can forget to pass history, and
a caller that forgets produces an agent that silently forgets the conversation while still
answering plausibly. Memory belongs to the layer that already knows the session id and the user.

## Definition of done

| Check | Command | Result |
|---|---|---|
| Formatting is clean | `python -m devtasks fmt` | **pass** |
| Lint is clean | `python -m devtasks lint` | **pass** |
| Types are clean | `python -m devtasks typecheck` | **pass** — `72 source files` |
| All tests | `python -m devtasks test-unit` | **pass** — `257 passed` |
| A fresh session can hold a proposal | `test_a_write_proposed_in_a_fresh_session_...` | **pass** |
| History keeps the newest turns | `test_history_keeps_the_newest_turns_when_bounded` | **pass** |
| A tool call survives the round trip | `test_a_stored_tool_result_rebuilds_as_a_tool_result` | **pass** |
| One key, one answer | `test_remember_updates_rather_than_appending` | **pass** |
| Expiry hides without destroying | `test_an_expired_fact_is_still_visible_to_a_reviewer` | **pass** |

## Findings worth recording

* **A test found a real bug in the code it was testing, and this is the good kind.** An assistant
  turn carrying a tool call stored *nothing* about that call: `append_all` read `message.name`
  and `message.tool_call_id`, but an assistant message carries its calls in `tool_calls` and
  leaves those two fields empty. Both columns were written as `NULL`, so reading the turn back
  produced an assistant message with no calls — and a provider replayed into the next turn would
  have seen a tool result answering a call it had no record of making. The conversation still
  looked sane, which is what made it worth a test.
  `_stored_call()` now reads whichever field the message actually uses. **Only the first call is
  kept**, and that limitation is on the function: one row has one pair of columns, so a
  three-call assistant turn cannot be represented completely. A row per call would make a turn's
  row count depend on the model's behaviour and force read-time reconstruction — the complication
  the flat schema exists to avoid. Documented rather than left silently lossy.
* **`rowcount` is not on the generic `Result`, and is unreliable where a dialect does expose
  one.** `forget` and `purge_expired` used it; both now select the affected ids and delete by id,
  which is type-correct and portable.
* **SQLite does not preserve `tzinfo`.** A stored timestamp comes back naive while `utc_now()` is
  aware, and comparing them raises `TypeError` — which reads like a logic error and is a driver
  detail. This is the third time this class of bug has appeared in the project; the test now
  compares two columns of the same row.
* **`_role()` raises on an unknown stored role rather than defaulting.** Defaulting to `user`
  would relabel an assistant turn as a request, producing a plausible wrong answer instead of an
  error. The roles are written by this service, so an unknown one means corruption.
* **`history()` selects descending and then reverses.** Selecting ascending and slicing returns
  the *start* of a long conversation and drops the current question — a bug that would look like
  the agent ignoring the user.
* **A session belonging to another user is returned, not rejected.** Rejecting it would turn a
  guessed session id into a distinguishable "this exists but is not yours" — the same oracle the
  approval lookup refuses to provide. The caller simply finds nothing in it.
* **A `utc_from_epoch` helper was removed before committing**, nothing having called it. Same
  reasoning as the `writer` property cut from Stage 8.

## Result

The agent has a conversation and a memory, and the write path works in a fresh session. The turn
stores the prompt and the final answer only — not the intermediate tool calls, which are reported
to the caller in the response and would otherwise grow every later prompt with text already acted
on. 257 tests, all four gates green.

## Carried forward

* **Facts are stored and recalled but never written by the agent.** `FactRepository` is complete
  and tested, and nothing calls `remember` yet: the loop does not extract facts from a
  conversation. Stage 11's UI (or a later extraction step) is where a caller appears. Recorded so
  this is a known gap rather than an oversight.
* **`DEFAULT_HISTORY_LIMIT` is 40 turns.** A fixed number, not a token budget, so a conversation
  of unusually long turns can still produce a large prompt. A token-aware bound is the honest
  fix and belongs with the observability work in Stage 10, where token counts become visible.
