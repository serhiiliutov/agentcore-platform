# Stage 4 — The ticketing client: errors, retries, idempotency, verification

* **Status:** complete
* **Started:** 2026-10-13
* **Estimate:** agent 5–7 h · human 2 days
* **Depends on:** Stage 1 (the contract suite), Stage 3 (the SDK spike)
* **Branch:** `main`

## Goal

Build the client behind the MCP server so that **a write happens at most once**, even
when the network, the mock's forced faults, and the client's own retry logic all try to
make it happen twice.

Stage 1 established the two facts that make this non-trivial, and this stage is the
response to them:

1. The mock has **no idempotency.** Posting the same payload twice creates two tickets,
   and nothing in either response distinguishes a retry from a genuine second request.
2. Its chaos middleware runs **before** the route handler. So a `429` or `500` provably
   wrote nothing — but a *timeout* is compatible with the write having succeeded.

Fact 2 is the one that makes a naive retry loop dangerous rather than merely wasteful.

## Deliverables

| Module | Responsibility |
|---|---|
| `errors.py` | HTTP response → exception, and the single retryability decision |
| `retry.py` | Backoff policy; **refuses** to retry a timeout |
| `idempotency.py` | Claim/complete/release ledger, atomic via SQLite's primary key |
| `models.py` | Typed shapes the MCP tool schemas are generated from |
| `client.py` | Transport: translates, retries, validates. No ledger, no approvals |
| `writes.py` | The guarded write path: claim, perform, and verify by reading back |

Tests: `tests/unit/test_error_taxonomy.py` (30), `tests/unit/test_guarded_writes.py` (18).

## The design in one paragraph

A write is recorded before it is attempted, not after. `reserve` claims a key
atomically — the `INSERT` *is* the lock — and only the holder of the claim performs the
call. On success the response is recorded so a repeat is answered from the ledger. On a
provably-pre-handler failure the claim is released, so a corrected retry is not blocked.
On a timeout the claim is **kept** and the API is read back: if the ticket is there, the
write is reported as recovered; if it is not, the claim stays `in_progress` so a repeat
reports the unknown state rather than writing a second ticket.

## Definition of done

| Check | Command | Result |
|---|---|---|
| Lint is clean | `python -m devtasks lint` | **pass** — `All checks passed!` |
| Formatting is clean | `python -m devtasks fmt` | **pass** — no diff |
| Types are clean | `python -m devtasks typecheck` | **pass** — `Success: no issues found in 36 source files` |
| Unit tests | `python -m devtasks test-unit` | **pass** — `140 passed` |
| A repeated key does not call the API twice | `test_repeating_a_key_replays_from_the_ledger_...` | **pass** |
| Two different keys still make two tickets | `test_a_different_key_creates_a_second_ticket` | **pass** |
| A timeout is never retried | `test_a_timeout_is_never_retryable` | **pass** |
| A resolved timeout reports `recovered` | `test_a_timeout_resolved_by_read_back_...` | **pass** |
| An undecidable write is not guessed at | `test_an_ambiguous_verification_is_...` | **pass** |

## Findings worth recording

* **The async tests run without a plugin marker.** The suite already had
  `asyncio_mode = auto` configured, so `async def test_...` is collected directly. Worth
  confirming rather than assuming — a silent skip would have made the whole timeout
  branch look tested when it was not.
* **A test-harness bug that three tests were hiding.** Seeding a ticket through
  `create_ticket_raw` consumed the queued fault and counted as a call *this client made*.
  Three tests therefore asserted the wrong thing — passing for the wrong reason, then
  correctly failing once the assertion was sharpened. `seed_ticket` and `seed_comment`
  now exist separately, because "a ticket created by an earlier process" and "a call this
  client made" are different facts and must not be indistinguishable.
* **The writer's dependency was wrong until the tests proved it.** `IdempotentWriter`
  originally required a concrete `TicketingClient`, which meant a scripted fake could not
  be passed without a cast. A cast would have silenced a real type mismatch instead of
  expressing the requirement. It now depends on `TicketingTransport`, a
  `runtime_checkable` Protocol, and `_assert_transport_conformance` statically proves the
  real client still satisfies it — so the fake cannot satisfy the protocol while the
  production class quietly does not.
* **`ruff check` and `ruff format` are different tools.** Confirmed the hard way in
  Stage 2. The gate now runs `fmt` then `lint`, every time.

## Honest limits, stated rather than glossed

Verification is a search, and searches are approximate. Two consequences follow, and
neither is hidden in the code:

* A ticket identical to the one being created, which already existed **before** this
  call, can be attributed to this call. The mock offers no way to ask "which ticket did
  *my* request create", so the client cannot distinguish those cases. The
  `client_request_id` is a ledger key, not something the API understands.
* Two equally-new matches are treated as **undecidable**, and the write is reported as
  unresolved rather than resolved by picking one. Picking wrong here is precisely how a
  duplicate gets mistaken for a success.

The consistent choice throughout is to surface "I do not know" rather than convert it
into a confident wrong answer.

## Result

A client that cannot double-write under the failures Stage 1 proved the mock can produce,
with the one genuinely undecidable case reported as undecidable. 140 tests, all hermetic,
green in under four seconds.

## Carried forward

* Stage 5 builds the four MCP tools on `MCPServer`, generating their schemas from
  `models.py` and routing writes through `IdempotentWriter`.
* The approval gate is **not** here. This client is what the MCP server uses *after* the
  agent's approval state machine has decided a write may happen.
* `idempotency.py`'s ledger is the MCP server's own store; the agent service's
  `IdempotencyKey` table (Stage 2) is a separate concern and is not this one.
