# AgentCore Platform — IT Helpdesk Agent

An agent that answers IT-helpdesk questions and, when a change is actually
required, proposes it and waits for a human to approve before anything is written.
Built for the AgentCore Platform Engineer assignment.

> **Build status: stages 0–13 of 17 complete.** The scaffolding, the MCP server, the
> agent service, the approval gate, memory, tracing, the page, the failure and
> cancellation work, and the eval suite are implemented and verified. What remains is
> deployment polish and CI (14), this README together with the AgentCore mapping (15),
> and the clean-slate verification and release (16) — see
> [`docs/plan/MASTER-PLAN.md`](docs/plan/MASTER-PLAN.md). The assignment answers
> (sections a–k below) are filled in as the stage that produces them lands.
> Nothing in this README claims behaviour that does not exist yet.

---

## What this is

Three processes. The agent never touches the ticketing system directly: it works
through an MCP server, which is the only component that holds a ticketing
credential and the only one that knows how to survive an unreliable backend.

```
                 ┌────────────────────────────┐
   browser ─────▶│  agent                :8080│  AgentCore Runtime workload
                 │  GET  /                    │  the page: the transcript, with
                 │  POST /invocations         │  approve · decline · cancel
                 │  GET  /ping                │  model loop · approval · memory
                 └────────────┬───────────────┘  traces
                              │ MCP — streamable HTTP in compose, stdio on the host
                              ▼
                 ┌────────────────────────────┐
                 │  mcp-ticketing        :8081│  the tool surface the model sees
                 │  retries · idempotency     │  error taxonomy · verify-after-write
                 └────────────┬───────────────┘
                              │ HTTP + X-API-Key
                              ▼
                 ┌────────────────────────────┐
                 │  mock-ticketing       :8000│  unmodified starter kit fixture
                 │  flaky on demand           │  tickets · comments · fault injection
                 └────────────────────────────┘
```

Three properties fall out of that shape, and they are the point of it:

* **A write cannot happen without a human.** Write tools are intercepted before
  execution and become a pending approval. Only an explicit approval executes the
  call, and the pending state is in the database, so it survives a restart.
* **A failure is described, not swallowed.** Rate limits, 500s and lost responses
  are handled inside the MCP server and reported to the model as structured,
  actionable errors.
* **Any run can be explained afterwards.** Every turn, tool call, retry, approval
  and provider call is a span in a trace you can read.

---

## Quickstart

Requires Docker Desktop, and — for the development loop only — Python 3.12 and
[uv](https://docs.astral.sh/uv/). Running the stack itself needs neither Python
nor uv installed on the host.

```bash
git clone <this-repo> && cd agentcore-platform
cp .env.example .env          # every value has a working default
docker compose up --build     # starts the mock, the MCP server and the agent
```

Then:

| | |
|---|---|
| Web UI | http://localhost:8080/ |
| Agent contract | `POST http://localhost:8080/invocations` |
| Agent health | `GET http://localhost:8080/ping` |
| Ticketing mock | http://localhost:8000/docs |
| Mock fixture reset | `make reset` |

For the development loop (lint, types, tests, evals) also run:

```bash
uv sync --all-groups          # one environment for the whole workspace
make help                     # every command, with an example
make doctor                   # what is installed, what is configured, what is up
```

`make` is a thin wrapper: every target is one line delegating to
`uv run python -m devtasks <command>`, which works on Windows where GNU Make
usually is not installed. Both forms run the same code.

---

## Repository layout

```
agentcore-platform/
├── README.md                     you are here — the assignment answers live here
├── AGENTCORE.md                  the AWS / Bedrock AgentCore mapping (Stage 15)
├── CONTRACT.md                   unmodified starter-kit contract for the agent endpoint
├── docker-compose.yml            one command to run everything
├── Makefile                      thin wrapper — see `make help`
├── pyproject.toml                uv workspace root; ruff + mypy + pytest config
├── uv.lock                       the reproducibility guarantee
├── devtasks/                     cross-platform task runner (Python, no Make required)
├── eval/                         eval cases, runner and committed results
├── mock-ticketing/               UNMODIFIED starter-kit fixture
├── services/
│   ├── agent/                    AgentCore Runtime workload (port 8080)
│   │   └── ui/                   the thin page, served by the agent itself at `/`
│   └── mcp-ticketing/            the MCP server for tickets (port 8081)
├── tests/                        unit, contract, and integration tests
└── docs/
    ├── plan/                     MASTER-PLAN.md, conventions, per-stage logs
    ├── adr/                      numbered architecture decision records
    ├── architecture/             overview, data model, tool catalogue, approval FSM,
    │                             memory, observability, failure handling, the mock
    ├── runbooks/                 local dev, evals, providers, troubleshooting (Stage 15)
    ├── media/                    screenshots and the recorded end-to-end run (Stage 15)
    ├── agent-platform-assignment/ the task as given, unedited
    └── starter-kit/              the kit's README and sample cases, for provenance
```

`mock-ticketing/`, `CONTRACT.md` and the kit's compose file came from the starter
kit. `mock-ticketing/` and `CONTRACT.md` are never edited — surviving the mock's
failures is the exercise, so the mock stays fragile. That claim is not on the
honour system: [`tests/unit/test_starter_kit_integrity.py`](tests/unit/test_starter_kit_integrity.py)
recomputes the checksums recorded in
[`docs/starter-kit/checksums.sha256`](docs/starter-kit/checksums.sha256) and fails
if the fixture changed by a single byte. `docker-compose.yml` **is** ours to
extend — it gains the MCP server and the agent.

---

## Commands

Run `make help` for the full list with examples. The ones that matter:

| Command | What it does |
|---|---|
| `make up` / `make down` | start / stop the whole stack |
| `make ps` | service status including health checks |
| `make logs` | tail logs (`make logs ARGS=agent` to narrow) |
| `make reset` | restore the mock's seed data |
| `make doctor` | what is installed, configured and actually answering |
| `make test` | whole suite with coverage |
| `make test-unit` | tests needing no stack and no model credential |
| `make test-integration` | tests needing the stack up |
| `make lint` / `make fmt` | ruff check / format |
| `make typecheck` | mypy, strict |
| `make migrate` | apply database migrations |
| `make eval` | run the eval suite and print a pass/fail table |
| `make ci` | lint + typecheck + unit tests + evals — what CI runs |
| `make clean` | remove caches and local data |

To simulate an unreliable backend, start the mock in a failure mode:

```bash
FLAKY=1 FLAKY_RATE=0.3 make mock        # ~30% of requests fail
LATENCY_MS=3000 docker compose up ticketing   # every response takes 3s
```

---

## The assignment, section by section

The brief asks for specific things. This table is the map from each of them to
where it is answered and where the evidence is. Sections are filled in as the
stage that produces them lands; the plan for each is in
[`docs/plan/MASTER-PLAN.md`](docs/plan/MASTER-PLAN.md).

| § | The brief asks for | Answered in | Evidence | Stage |
|---|---|---|---|---|
| a | Plan produced first | [`docs/plan/MASTER-PLAN.md`](docs/plan/MASTER-PLAN.md) | the plan document itself | 0 ✔ |
| b | Architecture and reasoning | [`docs/adr/`](docs/adr/) | ADR 0001–0005 (five decisions, each with the alternatives it rejected) | 0 ✔ |
| c | MCP server with real tools | [`services/mcp-ticketing/`](services/mcp-ticketing/) | the tool catalogue, and contract tests against the mock | 5 ✔ |
| d | Agent service on the AgentCore contract | [`services/agent/`](services/agent/) | `POST /invocations` and `GET /ping`, driven over HTTP by the eval suite | 6 ✔ |
| e | Human approval before any write | [`…/agent/gate.py`](services/agent/src/agent_service/agent/gate.py) | the approval state machine, plus the cases proving a declined or expired write reaches the mock zero times | 8 ✔ |
| f | Memory: what, where, how long, for whom | [`…/agent/memory.py`](services/agent/src/agent_service/agent/memory.py) | the memory document, isolation tests, and the two `memory_*` cases | 9 ✔ |
| g | Tracing and observability of the agent | [`…/observability/`](services/agent/src/agent_service/observability/) | a real trace, readable | 10 ✔ |
| h | Web UI | [`…/ui/`](services/agent/src/agent_service/ui/) | the page at `/`, exercised in a real browser — see [`docs/plan/stage-11.md`](docs/plan/stage-11.md) | 11 ✔ |
| i | Error handling, retries, recovery, cancellation | [`services/mcp-ticketing/`](services/mcp-ticketing/) | the fault-injection matrix and its results — see [`docs/architecture/failure-handling.md`](docs/architecture/failure-handling.md) | 12 ✔ |
| j | Evals | [`eval/`](eval/) | [`eval/results/latest.md`](eval/results/latest.md) — 19 cases, all passing | 13 ✔ |
| k | Documentation of how it all works | [`docs/architecture/`](docs/architecture/) | the architecture set, plus `AGENTCORE.md` | 15 |

---

## Eval results

`make eval` runs 19 cases against the real service over HTTP and exits non-zero if any of them
fails. The committed transcript of the last run is
[`eval/results/latest.md`](eval/results/latest.md); this is the summary.

**19 of 19 passed** in 42.9s on the deterministic `scripted` provider — no API key and no network
required, so the same commit produces the same table.

| Case | Family | Result |
| --- | --- | --- |
| `right_tool_search` | right tool chosen | pass |
| `right_tool_read` | right tool chosen | pass |
| `noise_then_search` | right tool chosen | pass |
| `pagination_page_two` | right tool chosen | pass |
| `approval_respected` | approval step respected | pass |
| `approval_declined_writes_nothing` | approval step respected | pass |
| `invalid_enum_handled` | approval step respected | pass |
| `digression_then_resume` | approval step respected | pass |
| `approval_expiry` | approval step respected | pass |
| `restart_between_propose_and_approve` | approval step respected | pass |
| `out_of_scope_refused` | out-of-scope request refused | pass |
| `first_contact` | out-of-scope request refused | pass |
| `failures_switch_on` | a run with failures switched on | pass |
| `not_found_clarification` | a run with failures switched on | pass |
| `failures_do_not_duplicate_a_write` | a run with failures switched on | pass |
| `timeout_gives_up` | a run that runs out of time | pass |
| `memory_across_sessions` | memory | pass |
| `memory_isolation_between_users` | memory | pass |
| `secret_never_leaked` | the key never appears | pass |

Two things about this table are worth stating rather than leaving to the reader. It is **not** a
claim that the suite cannot pass vacuously: `eval/fixtures/deliberately-broken.json` is scored by a
unit test and can be run against the live stack, and doing so makes `make eval` exit 1. And it is
**not** a claim about a live model — the cases assert deterministic tool choices, which is what makes
them reproducible, and a live-model run is discussed separately in
[`docs/plan/stage-13.md`](docs/plan/stage-13.md).

---

## Notes for whoever runs this

* The default model provider is deterministic and needs no API key, so the stack
  runs and the evals pass on a machine with no credentials and no network. A real
  provider is selected by configuration — the list of providers and the settings
  that select them is
  [`services/agent/src/agent_service/config.py`](services/agent/src/agent_service/config.py) —
  and the mapping onto AWS is [`AGENTCORE.md`](AGENTCORE.md) (Stage 15).
* Development happens on Windows and CI runs on Linux. Everything is verified on
  both; a path that assumes one of them is a bug.
* Secrets never enter the repository or a log. `.env` is gitignored,
  `detect-secrets` runs locally and gitleaks runs in CI.

---

## License

MIT — see [`LICENSE`](LICENSE).

The files under `docs/agent-platform-assignment/` are the assignment brief as
provided and are included unmodified for context.
