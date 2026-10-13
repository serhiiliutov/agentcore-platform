# Stage 2 — The database: schema, migrations and settings

* **Status:** complete for the schema; the repository layer is reassigned (see below)
* **Started:** 2026-10-13
* **Estimate:** agent 2–3 h · human 1 day
* **Depends on:** Stage 0
* **Branch:** `main`

## Goal

Give the agent service a real, migrated schema before any behaviour depends on it —
so that "an approval survives a restart" is a property of the database rather than
something the loop is trusted to remember.

## Deliverables

- [x] `config.py` — every setting from the environment, provider keys as `SecretStr`
- [x] `db/base.py` — engine, session factory, declarative base, SQLite pragmas
- [x] `db/models/conversation.py` — `ChatSession`, `Message`
- [x] `db/models/approval.py` — `Approval`, `ApprovalState`, `TERMINAL_STATES`
- [x] `db/models/memory.py` — `UserFact`
- [x] `db/models/operations.py` — `IdempotencyKey`, `ProviderSetting`
- [x] `db/models/observability.py` — `Trace`
- [x] `db/models/__init__.py` — the single place the schema is enumerated
- [x] `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`
- [x] `alembic/versions/0001_baseline_schema.py` — all seven tables

**Reassigned to a later stage, deliberately:** the `db/repositories/` layer moves to
the stages that actually read and write each aggregate — memory repositories to Stage
9, approvals to Stage 8. Reason: a repository written before its caller exists has no
requirements to satisfy, so it would have been shaped by guesswork and rewritten
anyway. The invariant it will enforce (every read of user-owned data takes a
`user_id`) is undisturbed; it simply lands with the code it constrains. Recorded here
and in the stage list of `MASTER-PLAN.md`.

## Definition of done

| Check | Command | Result |
|---|---|---|
| Lint is clean | `python -m devtasks lint` | **pass** — `All checks passed!` (one `RUF002` en dash in a docstring, rewritten as prose) |
| Types are clean | `python -m devtasks typecheck` | **pass** — `Success: no issues found in 28 source files` |
| Baseline migration applies | `python -m devtasks migrate` | **pass** — `Running upgrade -> 0001, baseline schema` |
| It reverses | `python -m devtasks downgrade base` | **pass** — `Running downgrade 0001 -> , baseline schema` |
| And re-applies | `python -m devtasks migrate` | **pass** — round-trip complete |
| All seven tables are created | autogenerate output | **pass** — `sessions`, `messages`, `approvals`, `user_facts`, `idempotency_keys`, `provider_settings`, `traces` |

## Findings worth recording

* **Autogenerate found every table and index on the first run.** The schema and the
  models agree, which is the point of generating rather than hand-writing the baseline.
* **`timezone = UTC` in `alembic.ini` breaks on Windows.** Alembic resolves it through
  the host's tz database and fails with `Can't locate timezone: UTC`, while the same
  value works on Linux. It only affects a timestamp comment in a generated file, so it
  is omitted with the reasoning written next to it — the kind of cross-platform
  difference ADR-0003 predicted and this stage confirmed.
* **SQLite needs `foreign_keys=ON` explicitly.** It is off by default, so a foreign key
  declared in a model does nothing at runtime and cascade deletes silently do not
  happen. `db/base.py` sets it on every connection, and the migration path connects
  through the same engine factory so a migrated database matches the one the app uses.
* **`Approval.client_request_id` is `UNIQUE`.** That is the database-level guarantee
  behind "a double execution cannot create two tickets", independent of the
  application logic that also tries to prevent it.

## Result

Seven tables, a working migration and its reverse, and a settings layer that cannot
leak a credential through a `repr()`. Verified by running it, not by reading it.

## Carried forward

* Repositories: Stage 8 (approvals) and Stage 9 (memory, sessions, facts).
* The trace index is written from Stage 10; the table exists now so that stage is not
  also a migration.
