# Stage 0 — Repository bootstrap, conventions and the plan

* **Status:** in progress
* **Started:** 2026-10-13
* **Estimate:** agent 1–1.5 h · human 0.5 day
* **Depends on:** nothing

## Goal

Produce a repository that a reviewer can clone, understand and run, with the
toolchain, conventions and plan already in place — before any product code exists.
Everything after this stage assumes these decisions, so getting them right first is
cheaper than retrofitting them.

## Deliverables

### Planning and conventions

- [x] `docs/plan/MASTER-PLAN.md` — scope, locked decisions, 17 stages, estimates, risks, definition of done
- [x] `docs/plan/00-conventions.md` — what is committed, commit style, test rules, code rules, secret policy
- [x] `docs/plan/stage-00.md` — this file
- [x] `docs/README.md` — documentation index and a reading order for a reviewer
- [x] `docs/adr/0001-monorepo-and-service-boundaries.md`
- [x] `docs/adr/0002-agent-orchestration-and-model-providers.md`
- [x] `docs/adr/0003-dependency-management-and-task-running.md`
- [x] `docs/adr/0004-persistence-and-migration-strategy.md`

### Repository layout

- [x] `mock-ticketing/` moved to the root, byte-identical to the starter kit
- [x] `CONTRACT.md` moved to the root
- [x] `docker-compose.yml` moved to the root (extended in Stage 14)
- [x] Starter-kit README and sample eval cases archived under `docs/starter-kit/`
- [x] `docs/agent-platform-assignment/` left untouched

### Toolchain

- [x] `.gitignore` — secrets, runtime data, internal notes, media policy
- [x] `.gitattributes` — LF everywhere, CRLF for Windows-only scripts
- [x] `.editorconfig`
- [x] `.dockerignore` — root build context, because the images share one `uv.lock`
- [x] `.env.example` — every knob documented, every value has a working default
- [x] `LICENSE` — MIT
- [x] `pyproject.toml` — uv workspace, ruff, mypy (`strict`), pytest, coverage
- [x] `uv.lock` — resolved and committed
- [x] `Makefile` — thin wrapper over `devtasks`
- [x] `devtasks/` — cross-platform task runner (`runner.py`, `tasks.py`, `__main__.py`)

### Service skeletons

- [x] `services/agent/pyproject.toml` — full dependency set declared up front
- [x] `services/agent/src/agent_service/__init__.py` — package map and design rules
- [x] `services/agent/README.md`
- [x] `services/mcp-ticketing/pyproject.toml`
- [x] `services/mcp-ticketing/src/ticketing_mcp/__init__.py`
- [x] `services/mcp-ticketing/README.md`

## Definition of done

Every check below was run and its real output is what is recorded here.

| Check | Command | Result |
|---|---|---|
| Workspace resolves and installs | `uv sync --all-groups` | **pass** — 79 packages resolved; `uv.lock` written; services installed editable |
| Lint is clean | `python -m devtasks lint` | **pass** — `All checks passed!` (6 findings fixed on the first run, all auto-fixable) |
| Types are clean | `python -m devtasks typecheck` | **pass** — `Success: no issues found in 10 source files` under `mypy --strict` |
| Unit tests pass | `python -m devtasks test-unit` | **pass** — 25 passed in 0.48s |
| The task runner works | `python -m devtasks help` | **pass** — every command listed with an example (asserted by the CLI tests) |
| The mock is unmodified | `python -m devtasks test-unit -k integrity` | **pass** — 9 tests; all six protected files match the recorded SHA-256 |
| Protected files are LF-only | byte scan of all six | **pass** — no CRLF, so the recorded hashes are stable across checkouts |
| `.env` is not committed | `git status` / `.gitignore` | **pass** — `.env` never existed; only `.env.example` is tracked |
| One meaningful first commit | `git log` | **pass** — see Result |

Two things the DoD deliberately does *not* require yet, because the services they
would exercise do not exist: `docker compose up` (the compose file still describes
the mock alone) and `make eval` (the runner arrives in Stage 13). `make ci`
therefore still fails at its last step by design, with a message that says so
rather than a silent pass.

## Notes and findings

* **`make` is not installed on this machine and `gitleaks` is not installed.**
  Both were checked rather than assumed. Consequences, both already reflected in
  the plan: the task logic lives in `devtasks/` with the `Makefile` as a wrapper,
  and secret scanning uses the pip-installable `detect-secrets` locally with the
  gitleaks GitHub Action as an independent second opinion in CI.
* **`uv` 0.7.10 and `poetry` 2.1.3 are both present.** uv was chosen; the reasoning
  and the rejected alternatives are in ADR-0003.
* **`gh` still reports `AIDataMaster` as the active account**, while the local git
  identity is already `serhiiliutov <sliutov@kse.org.ua>`. Nothing is published
  until this is resolved at Stage 16, before the first push.
* **Development hardware measured, not assumed:** Intel i7-8750H, 15.9 GB RAM,
  GTX 1050 Ti with 4 GB VRAM, 706 GB free on `D:`. No GPU pass-through into Docker
  on Windows. This is what makes the local-model position in ADR-0002 concrete: a
  ~3B model on CPU is a swappability demonstration, never the demo path.
* **The `mcp` SDK resolved to 2.3.0**, a major version above the 1.x line the
  examples in the wild mostly target. Stage 3 is a deliberate spike on exactly
  this, scheduled before any agent code depends on the SDK, with a documented
  stdio fallback if the streamable-HTTP client path misbehaves.

## Result

Stage 0 is complete and verified. The repository is a working skeleton: a uv
workspace whose single lock file resolves both services, a task runner that works
on Windows and Linux, a strict type and lint gate that is already green, an
integrity test that pins the fixture, and the plan and decision records that every
later stage builds on.

Recorded honestly, including the parts that were wrong the first time:

* The first `uv sync` installed **only** the dev tooling — 23 packages — because
  nothing in the root project depended on the workspace members. Fixed by
  referencing both services from the root dev group; the second sync installed the
  service dependencies and proved the resolution actually covers them.
* `mypy --strict` failed with `Duplicate module named "__main__"` before any
  product code existed. The cause was `files = [..., "tests", "eval", ...]` naming
  directories that did not exist yet, so mypy fell back to basename-only module
  names. Fixed with `explicit_package_bases` and `namespace_packages`, and by
  creating the directories. Worth recording because the fix is the kind of thing
  that gets papered over with an `--exclude` and then bites later.
* The first attempt at proving the mock untouched compared against
  `docs/agent-platform-assignment/starter-kit/`, which no longer exists — the kit
  was *moved* into the root, not copied. `Compare-Object` against an empty list
  reported "IDENTICAL" anyway, which is exactly the false pass the integrity test
  now makes impossible.

Nothing is claimed about behaviour that is not yet built.

## Carried forward

* Nothing is deferred from this stage. The Dockerfiles, compose services and the
  real `README.md` (assignment sections a–k) belong to Stages 11, 14 and 15.
</content>
