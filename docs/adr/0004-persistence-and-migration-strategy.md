# ADR-0004 — Two persistence styles, chosen per service

* **Status:** accepted
* **Date:** 2026-10-13
* **Deciders:** Serhii Liutov
* **Related:** ADR-0003 (dependency management)

## Context

Two services need to persist something, and the two things are not alike.

**The agent service** keeps state that is the *subject* of the assignment:

| Table | Rows | Changes over time |
|---|---|---|
| `sessions` | one per conversation | keyed by session id, touched every turn |
| `messages` | many per session | append-only, windowed |
| `approvals` | few, but critical | a state machine: pending → approved/declined/expired/executed |
| `user_facts` | few | key/value with a TTL and a source |
| `idempotency_keys` | few | client request id → the result it produced |
| `traces` | many | indexed by request, written after the fact |
| `settings` | one per scope | which provider and model is selected |

These schemas change as the work progresses — the approval table gained a TTL and
an executor result during design, and the migration story has to survive that.
They also have relationships, invariants and queries worth expressing.

**The MCP server** keeps one thing: an idempotency ledger, mapping a
`client_request_id` to the JSON result of the write it produced. Two columns of
substance, no relationships, no queries beyond a primary-key lookup, and a shape
that has not changed since it was designed.

## Decision

Use each service's persistence tooling in proportion to its data model.

**Agent service — SQLAlchemy 2.0 (typed ORM) + Alembic.**

* Models are typed; `mypy --strict` covers the schema.
* Every migration is a reviewable file with a working `downgrade`, so a schema
  change is visible in a diff instead of hiding in an `if not exists` guard.
* One repository layer owns every query, which is what makes the "a user never
  sees another user's memory" claim enforceable rather than aspirational: the
  repository's read methods all take a `user_id` and there is no code path that
  omits it.
* Default is SQLite in WAL mode on a mounted volume — zero configuration, one file
  to inspect, perfectly adequate for this workload. PostgreSQL is available as a
  compose profile and is exercised once, so the claim that `DATABASE_URL` is a
  real switch is tested rather than asserted.

**MCP server — the standard library's `sqlite3`.**

* A single table, created with `CREATE TABLE IF NOT EXISTS` at startup, with
  `client_request_id` as the primary key.
* No ORM, no migration tool, no second lock file of database concerns.
* Writes are `INSERT OR IGNORE` followed by a `SELECT`, which is the whole
  concurrency story for this table: whichever attempt gets there first wins and
  the other reads the winner's row.

## Alternatives considered

**SQLAlchemy + Alembic in both services.** Consistent, and consistency has value.
Rejected as ceremony without benefit: it would add an Alembic chain, an `env.py`
and a migration directory to a service whose entire schema is four lines, and the
only migration it would ever need is the one that creates the table.

**Stdlib `sqlite3` in both services.** Simpler and consistent the other way.
Rejected: hand-writing the agent's schema and its evolution means either an
`ALTER TABLE` ladder in application code or a schema that silently diverges from
the models. The agent's tables *will* change during development; that is precisely
when a migration tool pays for itself.

**A JSON file per service.** Tempting for something this small. Rejected on two
counts: an approval that must survive a restart is exactly the thing that must not
be rewritten by a concurrent access, and a partial write on a JSON file is a
corrupted approval. SQLite gives us atomicity for free.

**PostgreSQL for everything, from the start.** The most production-like option.
Rejected as the default because it costs a service, a health check and a startup
dependency for a workload that fits comfortably in one file, and because the
assignment explicitly asks for a small mock. It is a supported profile rather than
the default so the switch is demonstrably real.

## Consequences

**Positive**

* The schema that matters is expressed once, in types, and its evolution is
  reviewable.
* The user-scoping invariant lives in one place, so memory isolation is testable
  with a query-shape test rather than by reading every call site.
* The MCP server stays small enough to read end to end — which matters, because it
  is the component that has to survive a hostile backend.

**Negative**

* Two database idioms in one repository. Mitigated by a comment at the top of the
  MCP server explaining why it differs, and by this ADR.
* Alembic must be run against SQLite and PostgreSQL; the migration chain is kept
  to portable operations with no database-specific types. A round-trip test
  (`upgrade head`, `downgrade base`, `upgrade head`) runs against a throwaway
  database in CI.
* SQLAlchemy's type annotations can fight `mypy --strict` in the ORM's more
  dynamic corners. If that happens, the relaxation will be narrow, scoped to
  model definitions, and recorded here rather than applied repository-wide.
</content>
<parameter name="task_progress">
- [x] Stage 0 — git init, starter kit relocated, root config files
- [x] Stage 0 — root pyproject.toml + workspace; full dependency set resolved and locked
- [x] Stage 0 — service packages: pyprojects, __init__ files, READMEs
- [x] Stage 0 — devtasks runner + tasks + CLI + Makefile
- [x] Stage 0 — docs index + conventions
- [ ] Stage 0 — ADR 0004 + stage-00 log
- [ ] Stage 0 — root README; lint + typecheck green; first commit
- [ ] Stage 1 — Mock ticketing contract tests
- [ ] Stage 2 — DB layer + Alembic baseline
- [ ] Stage 3 — MCP transport spike (SDK is mcp 2.3.0 — API must be verified)
- [ ] Stage 4 — Ticketing client: errors, retries, idempotency, verify
- [ ] Stage 5 — Four MCP tools + tool catalog
- [ ] Stage 6 — Agent service skeleton (AgentCore contract)
- [ ] Stage 7 — Provider abstraction + secret hygiene
- [ ] Stage 8 — Agent loop + structural approval gate
- [ ] Stage 9 — Memory (session + user facts)
- [ ] Stage 10 — Observability
- [ ] Stage 11 — Web UI service
- [ ] Stage 12 — Failure hardening + cancellation
- [ ] Stage 13 — Evals
- [ ] Stage 14 — Deployment polish + CI
- [ ] Stage 15 — README (a–k) + AGENTCORE.md + media
- [ ] Stage 16 — Clean-slate verification + tag + push
