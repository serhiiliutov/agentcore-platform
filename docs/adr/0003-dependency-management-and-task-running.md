# ADR-0003 — uv workspaces for dependencies, and a Python task runner beside the Makefile

* **Status:** accepted
* **Date:** 2026-10-13
* **Deciders:** Serhii Liutov
* **Related:** ADR-0001 (service boundaries), ADR-0004 (persistence split)

## Context

There are two Python services (plus a dev-tooling package and an eval runner) in
one repository with different dependency sets. Four constraints matter:

1. **Reproducibility.** A reviewer clones this and expects the versions we tested.
2. **Small runtime images.** Both services need `pydantic`; only the agent needs
   the model SDKs and `boto3`. A shared environment is right for development and
   wrong for a container.
3. **Speed.** Container builds and CI runs reinstall dependencies constantly, so
   the resolver and installer are on the critical path of every build.
4. **Cross-platform development.** Development happens on Windows, CI runs on
   Linux. Anything that works on only one of those is a latent failure.

## Decision

### Dependencies: `uv`, in workspace mode

* A single root `pyproject.toml` defines a **uv workspace** with
  `services/agent` and `services/mcp-ticketing` as members.
* One `uv.lock` covers everything and is committed — it is what makes two machines
  agree.
* `uv sync --all-groups` gives a developer the whole workspace in one virtual
  environment, members included. The members are referenced from the root's dev
  group, because uv otherwise installs only the root project and its dev tooling.
* Runtime images install one package and no dev tooling:
  `uv sync --frozen --no-dev --package agent-service`.
* `requirements.txt` is deliberately **not** the source of truth. `uv export`
  produces an equivalent file for a pip-only reviewer; the lock is the contract.

### Task running: `devtasks` (Python) wrapped by a `Makefile`

* All task logic lives in `devtasks/`, run as
  `uv run python -m devtasks <command>`.
* The root `Makefile` is a thin wrapper — every target is one line delegating to
  `devtasks`, so `make lint` and `python -m devtasks lint` are the same code path.
* CI runs the `make` targets, which is what stops the Makefile from rotting
  unnoticed as the task list changes.

## Alternatives considered

**poetry.** Installed here (2.1.3) and perfectly capable. Not chosen: a shared
multi-package workspace with a single lock needs extra configuration, and its
resolve/install path is materially slower on the cold-cache builds that dominate
CI. Its lock format is not better than uv's, only older.

**Plain pip with pinned `requirements.txt` files.** Universally understood and the
lowest barrier for a reviewer. Rejected as the source of truth: hand-pinned files
drift, transitive dependencies go unpinned, and there is no cross-service
resolution to catch a conflict between the two services. It survives as an export
target, not as the contract.

**No lock file.** Cheapest to write, worst to review. A submission that cannot be
rebuilt identically in six months is not engineering.

**Make only, no Python task runner.** Conventional, and CI is Linux where `make`
exists. Rejected because it fails on the machine we develop on: GNU Make is not
installed on this Windows checkout (verified), so the documented entry point would
be unusable for the person doing the work — and it would rot silently, because
nobody would ever exercise it locally. The wrapper keeps the convention and
removes the failure mode.

**`nox` or `tox`.** Real tools with real value for matrix testing. Rejected as
overkill: we need a handful of commands, not an environment matrix, and each would
add a dependency to run commands that are already one subprocess each.

## Consequences

**Positive**

* `git clone && uv sync && make up` is the whole setup, on Windows or Linux.
* One lock file means a conflict between the two services is a resolve error, not a
  production surprise.
* Runtime images stay small because one package can be synced on its own.
* The Makefile serves humans and CI; `devtasks` provides correctness. Neither can
  drift from the other without the drift being visible.

**Negative**

* uv is a non-standard tool a reviewer may not have. Mitigated by stating it in the
  first line of the setup instructions, by the `requirements.txt` export path, and
  by the fact that *running* the stack needs no uv at all — only the dev loop does.
* Two entry points for the same commands is one more thing to keep in step.
  Accepted: the one-line delegation keeps the cost per command trivial.
* A workspace means one member's bump can affect another's resolve. Accepted here —
  for a two-service repo, finding that out early is the point.
