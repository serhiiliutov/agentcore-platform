# The data model

Seven tables, one SQLite file, Alembic migrations. This document is what each table is for, the one
invariant the schema exists to enforce, and the two places a reader might expect a table and will
not find one.

The schema is declared once, in `services/agent/src/agent_service/db/models/`, and the migration in
`alembic/versions/0001_baseline_schema.py` creates it.

## The seven tables

```
sessions ──┬── messages
           └── approvals

user_facts          (no foreign keys — scoped by user_id alone)
traces
idempotency_keys
provider_settings
```

### `sessions` and `messages` — short-term memory

`sessions.id` **is the AgentCore session id**, taken verbatim from the
`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header rather than generated. Generating our own id
would mean maintaining a mapping for no benefit, and would break the contract's promise that the
same header value resumes the same conversation.

`messages` holds the turn-by-turn history: `role` (`system`/`user`/`assistant`/`tool`), `content`,
the optional `tool_name` and `tool_call_id`, and a **`sequence`** counter. Ordering by `created_at`
alone would be a bug waiting to happen, since two turns can share a timestamp; `sequence` is
monotonic within a session and the index is `(session_id, sequence)` because the common read is "the
last N turns of one session, oldest first".

### `approvals` — the write that needs a human

The table the assignment's central guarantee lives in, described in full in
[`approval-state-machine.md`](approval-state-machine.md). What matters at the schema level:

- `action` + `payload` store the tool and the **exact** arguments, so execution never re-derives
  parameters from a model.
- `state` has a `CHECK` constraint listing the legal values, so a typo fails at the database.
- `client_request_id` is **unique**, which is what makes a double execution impossible even under a
  retry.
- Two indexes match the two real queries: `(session_id, state)` for "is anything pending here?", and
  `(state, expires_at)` for "which pending approvals have expired?".

### `user_facts` — long-term memory

One scalar value per key, per user. See [`memory.md`](memory.md) for the design; the schema-level
points are the `UNIQUE (user_id, key)` constraint, which makes memory a lookup rather than a log —
storing the same preference twelve times and hoping the read picks the right one is a bug generator
— and `expires_at`, which is part of the read index so an expired fact is never loaded and then
filtered.

### `traces` — the index, not the trace

One row per invocation: `session_id`, `user_id`, `started_at`/`ended_at`/`duration_ms`, `status`,
`provider`, `model_id`, counters (`model_turns`, `tool_calls`, `total_tokens`), `error`, and
`trace_path`. Three indexes, each answering a question somebody actually asks: what happened in this
session, what has this user been doing, what has been failing.

The spans are **not** here — see below.

### `idempotency_keys` — the agent's own ledger

Separate from the MCP server's ledger on purpose. AgentCore retries `/invocations` on a timeout, and
a retried invocation that was not recognised would run the model again (paying twice) and might
propose a second write the user already approved once. This renders a retried request into the same
response as the first.

`request_fingerprint` is what makes it honest: keying on the id alone would collapse two genuinely
different requests that happen to share an id into one answer, so a hash of the prompt is stored
alongside and a mismatch is an error rather than a silent replay of the wrong answer. `expires_at`
is indexed because the ledger is pruned by age — an unbounded ledger is a slow leak.

### `provider_settings` — which model, for which scope

A row rather than process state, so the selection survives a restart and is visible to a reviewer.
`scope` is `global` or `session:<id>`, which allows a per-session override without inventing a
second mechanism. It stores the *selection*, never a credential: keys live in the environment.

## The one invariant: ownership

> **No query returns another user's data.**

Every user-scoped table carries `user_id`, and every repository method requires it — there is no
unscoped read path. `messages` **denormalises** `user_id` even though it is reachable through the
parent session. Strictly that is redundant; it is there so the invariant is checkable without a
join, and so a message query that forgot to scope would be visibly wrong rather than silently
correct-looking. `traces`, `user_facts` and the ledger all carry it for the same reason.

The consequence is that "one user never sees another user's memory" is a property of the schema
rather than of a filter somebody remembered to write, and
`tests/unit/test_agent_memory.py` asserts it with two users.

## Two things deliberately not in the database

**The spans.** A trace is twenty to two hundred structured events, some carrying tool payloads.
Storing them as rows would mean either a loose JSON column (a file with extra steps) or a span table
whose shape has to anticipate every future event type. Instead each run writes a JSONL file under
`TRACE_DIR` and this schema holds the searchable summary. `trace_path` is stored so a row is
self-sufficient — given a row, the spans can be opened without guessing a filename convention from
the id. The reasoning is repeated in the model's own docstring because it is the kind of decision
that looks like laziness unless the alternative is spelled out; see
[`observability.md`](observability.md).

**The in-flight runs.** Cancelling a turn needs to reach a running coroutine in *this* process, so
the registry is a dictionary keyed by run id (`agent/inflight.py`), not a table. A row would tell
some other worker — or a restarted one — to stop a run it does not have, and the token it names
would not exist there. A run a restart killed cannot be cancelled because it is already over.

## Migrations

Alembic, with the baseline in `alembic/versions/0001_baseline_schema.py` and the container's
entrypoint applying `alembic upgrade head` before the service starts. Two notes:

- **`create_all` is not the deployment path**, it is the test path. Stage 8 found a real defect this
  way: a column declared `index=True` *and* given an explicit `Index` in `__table_args__` produced
  two indexes under one name, which `create_all` rejected and the migration never noticed, because
  nothing had called `create_all` on that metadata until the tests did.
- **On a managed database the migration moves out of the entrypoint.** A runtime that starts
  instances on demand would race N instances running `alembic upgrade head` at once, which is item 2
  in the "what I would change" table of [`AGENTCORE.md`](../../AGENTCORE.md).

## What is deliberately absent

- **No user table.** Users are ids; there is no directory to join to and no profile to keep. Memory
  is a fact store, not an account system.
- **No deletion path for facts, only expiry.** A fact stops being returned when it expires and is
  kept, so a reviewer can see what was once believed and when it stopped applying. A real deployment
  needs a "forget me" path, and that is a gap named in the same section of `AGENTCORE.md`.
- **No table for the mock's data.** The fixture owns its own state in memory and resets from
  `seed.py`; nothing about it is ours to model.
