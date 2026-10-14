# Runbook: local development

How to get the stack running, how to run parts of it on the host, and what to do when the dev loop
and the containers disagree. For failures rather than workflows, see
[`troubleshoot.md`](troubleshoot.md).

## Prerequisites

| | Needed for | Notes |
|---|---|---|
| **Docker Desktop** | running the stack | the only hard requirement for the demo; 4 GB RAM free is plenty |
| **Python 3.12** | the dev loop (lint, types, tests, evals) | via `uv`, which installs the interpreters it needs |
| **uv** | the dev loop | `pipx install uv`, or the installer from astral.sh |
| **GNU Make** | optional | every target is a one-line wrapper; `python -m devtasks` is the same thing |

The stack itself needs **neither Python nor uv on the host** — the containers carry their own. That
is deliberate: a reviewer should be able to run the demo on a machine with nothing but Docker.

## First run

```bash
git clone https://github.com/serhiiliutov/agentcore-platform && cd agentcore-platform
cp .env.example .env          # every value has a working default
docker compose up -d --wait   # builds, starts, and blocks until every healthcheck passes
```

`--wait` is the whole definition of done in one command: it exits 0 only when all three services
report healthy, and non-zero otherwise. If it hangs or fails, go to
[`troubleshoot.md`](troubleshoot.md) — the failure is in the stack, not in your configuration.

Then open <http://localhost:8080/>, type a problem, and watch the tool calls. The mock's own API
docs are at <http://localhost:8000/docs>.

## The dev loop

```bash
uv sync --all-groups      # one environment for the whole workspace
make help                 # every command, with an example
make doctor               # what is installed, what is configured, what is actually answering
make test-unit            # 376 tests; no stack, no key, no network
make lint && make typecheck
```

`make` and `uv run python -m devtasks` are interchangeable. On Windows, where GNU Make is often
absent, the second form always works:

```bash
uv run python -m devtasks test-unit -k approval -x     # trailing args go to the underlying tool
```

## Two stacks, deliberately

There is the **development** stack and the **CI** stack, and they differ only in the deterministic
settings CI needs (`FLAKY=0`, `LATENCY_MS=0`, `agent_model_provider=scripted`). They cannot run at
the same time because they publish the same ports, so:

```bash
make down && make up-ci      # switch to the CI stack (its own compose project)
make ps-ci                   # status, with healthchecks
make down-ci                 # stop it (add ARGS=-v to drop its volumes)
```

Keeping them separate is why `up-ci` cannot stop or reuse your development containers, and why the
CI stack's database is a different file. A stack only CI can start is a stack only CI can debug —
so the CI settings are runnable locally, which is the point.

## Running parts on the host

The containers are the supported path; running on the host is for debugging and for the fast loop.
The three services have separate tasks:

```bash
make mock        # the ticketing mock alone, on 127.0.0.1:8000 (blocking)
make serve       # the agent alone, against a mock on the host (blocking; page at /)
make mcp         # the MCP server alone, over stdio — what the tests use
```

`make serve` sets three things you would otherwise have to remember, and each is a real trap:
`MCP_TRANSPORT=stdio`, `TICKETING_BASE_URL=http://127.0.0.1:8000` and a working directory of
`services/agent/`. The `.env` values are written for the compose network — the MCP server is
reachable as `ticketing` only from inside it — and the database path is relative, so a run from the
repository root would open a *different, empty* database and fail with a missing table.

**Migrations** are applied automatically by the container's entrypoint. On the host:

```bash
make migrate      # alembic upgrade head
make revision ARGS='-m "add something"'
make downgrade ARGS=base
```

## Data, and clearing it

| What | Where (host) | Where (container) |
|---|---|---|
| Agent database | `data/agent.sqlite3` | the `agent-data` volume |
| Traces | `data/traces/*.jsonl` | the same volume, under `/data` |
| MCP write ledger | `data/mcp/*` | the `mcp-data` volume |
| Mock tickets | in process memory | re-seeded on restart |

```bash
make reset                       # restore the mock's seed data, mid-conversation if you like
docker compose down              # stop, keep the volumes
docker compose down -v && make up  # stop and wipe: a clean slate
make clean                       # host-side: caches and data/, never source
```

`make clean --deep` also removes `.venv`. It never touches source or the git tree.

## Failure modes worth having on purpose

The assignment asks for a mock that fails some of the time, and the fastest way to understand the
error taxonomy is to watch it happen:

```bash
FLAKY=1 FLAKY_RATE=0.3 FLAKY_SEED=7 make mock     # ~30% of calls fail, reproducibly
LATENCY_MS=3000 docker compose up ticketing       # every response takes 3s, to exercise the deadline
FLAKY=1 FLAKY_RATE=1.0 docker compose up ticketing  # everything fails, as in docs/media/05
```

`FLAKY_SEED` is what makes a flaky run reproducible, which is what lets a test assert on a run that
actually failed. [`../architecture/the-mock.md`](../architecture/the-mock.md) explains each mode and
what the agent is expected to do about it.

## Regenerating the captured run

```bash
make up-ci                     # a healthy stack: frames 01–04
make screenshots

make down-ci
FLAKY=1 FLAKY_RATE=1.0 make up # a failing mock: frame 05
make screenshots ARGS=--failure
```

The driver talks to a real browser over the DevTools Protocol (`devtasks/chrome.py`); it needs Chrome
or Edge installed, or `CHROME_PATH` pointing at one. See
[`../media/README.md`](../media/README.md).
