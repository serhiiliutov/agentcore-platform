# AgentCore Platform Engineer Assignment — Master Plan

> Owner: Serhii Liutov · Repo: `serhiiliutov/agentcore-platform`
> Status: **locked, stage-by-stage execution** · This file is the single source of truth for scope,
> sequencing and time. Tick progress in the checklists per stage as we complete them.

---

## 1. Scope

Build the **IT Helpdesk Agent** (Assignment Option 1) as a local, containerised, observable agent
system, plus the written **AgentCore design doc**. No AWS account, no cloud spend. Everything runs
with one `docker compose up`.

### 1.1 Assignment deliverables → where they live

| # | Deliverable | Where |
|---|---|---|
| 1 | Code in a GitHub repo | `serhiiliutov/agentcore-platform` |
| 2 | README a–k (setup, architecture, agent design, tool design, quality, observability, decisions, standards, AI usage, next steps) | `README.md` + `docs/` |
| 3 | AgentCore design doc | `AGENTCORE.md` |
| 4 | Screenshots / recording of a full run + eval output | `docs/media/`, `eval/results/latest.md` |

### 1.2 Hard requirements → implementation

| Requirement | Implementation | Stage |
|---|---|---|
| HTTP agent service, swappable model provider | `POST /invocations` + `GET /ping` per `CONTRACT.md`; `ModelProvider` protocol with 5 adapters, selectable at runtime | 6, 7 |
| Tools behind an **MCP server we write** (search paginated, read, write), agent reaches mock *only* through it, authenticated | FastMCP server, 4 tools, `X-MCP-Token` inbound / `X-API-Key` outbound | 3, 5 |
| Approval before the write; pending approval survives restart | Structural gate in the tool executor (not a prompt instruction) + SQLite `approvals` state machine | 8 |
| Memory: per session, plus ≥1 cross-session fact, isolated per user | `messages` (windowed) + `user_facts` (whitelisted keys, TTL, always user-scoped) | 9 |
| Failure handling: retries, give up on timeout, tell the user, never double-write | Retry/backoff honouring `Retry-After`, deadline budget, read-back verification, idempotency keys | 4, 12 |
| Evals runnable with one command | `make eval` driving the real service over HTTP, `eval/results/latest.{md,json}` | 13 |
| Traces per request (model + tool calls, latency, tokens, errors) | Own span model → JSONL + SQLite + `GET /api/traces/{id}` | 10 |
| Thin web page showing tool calls and approving/declining | `services/web` (nginx + vanilla ES modules), Approve/Decline/Cancel, memory + trace panels | 11 |
| Optional extra: **at most one** | **Request cancellation** (chosen — reinforces the durability/timeout story). Streaming is the documented runner-up. | 12 |

---

## 2. Locked decisions (with the reasoning; full ADRs land in `docs/adr/`)

| Decision | Choice | Why | Rejected |
|---|---|---|---|
| Language | **Python 3.12** | The mock is FastAPI; MCP, SQLAlchemy and the OTel ecosystem are first-class in Python | TypeScript |
| Orchestration | **Plain, explicit agent loop** | The scored qualities are state, approval durability and traceability — a framework hides exactly those. ~250 lines we fully control and can unit-test | LangGraph (heavy, graph state we'd have to trace anyway), Strands, OpenAI Agents SDK — recorded in ADR-0002 as considered |
| MCP server | **FastMCP** (official Python SDK), stdio **and** streamable-HTTP transports | HTTP is right for containers; stdio makes tests and local dev trivial. One flag selects | Custom JSON-RPC |
| Dependencies | **uv** (workspace, `uv.lock`, `uv sync --frozen`) | Fastest resolve/install, real lockfiles, native multi-service workspace, best Docker layer caching. Also `uv export` emits `requirements.txt` so a reviewer can use plain pip | Poetry (slower, heavier), bare pip (no lockfile) |
| Persistence | **SQLAlchemy 2.0 + Alembic**, SQLite (WAL) by default, `DATABASE_URL` → Postgres | Six evolving tables (sessions, messages, approvals, user_facts, idempotency, traces); schemas change across stages 8–12, so migrations are earned, not decorative | Hand-rolled DDL, JSON files |
| Model providers | `anthropic`, `openai`, `openai_compatible` (Ollama/vLLM/LM Studio/Groq), `bedrock`, plus deterministic `scripted` and `replay` | One generic OpenAI-compatible adapter covers every local/self-hosted runtime. `scripted` + `replay` make CI and evals key-free and reproducible | Hard-coding one vendor |
| Provider/key exposure | **Keys stay server-side in env.** UI may *select* among configured providers and shows `configured: true/false`. An opt-in `ALLOW_RUNTIME_KEY_ENTRY=1` dev mode accepts a key **in memory only** (never DB, logs, traces or responses) | Pasting a provider key into a browser is a real security smell in a reviewed deliverable; this keeps the flexibility without the leak surface | Keys in the UI by default, keys in the DB, keys in localStorage |
| Frontend | **Separate service**, nginx + vanilla ES modules (no build step, no framework) | The assignment scores agent engineering, not UI ("we don't score visual design"). Separate service keeps the agent image a *pure* AgentCore Runtime workload. The HTTP API is framework-agnostic, so a React app can be added later with zero backend change | React/Next (build step + CI weight for no scored benefit) |
| Optional extra | **Cancellation** | Directly reinforces "behaves correctly when the backend fails" — a cancelled run must provably perform no write | Streaming (easier, less informative) |
| Tracing | Own span model → JSONL + SQLite index, optional OTLP export behind a flag | Zero-dependency, greppable, and works with no collector; OTel/Langfuse is the documented swap-in | Langfuse-only (extra service, extra failure mode) |
| Container target | ARM64-capable multi-stage images, `--platform linux/arm64` documented | AgentCore runs ARM64; local testing is amd64 — stated honestly in the README per `CONTRACT.md` | — |

---

## 3. Repository layout (target)

```
agentcore-platform/
├─ README.md                     # deliverable 2 (a–k)
├─ AGENTCORE.md                  # deliverable 3
├─ CONTRACT.md                   # provided AgentCore Runtime contract
├─ LICENSE                       # MIT
├─ Makefile                      # up/down/lint/fmt/typecheck/test-unit/test-integration/eval/ci
├─ pyproject.toml                # uv workspace root + ruff/mypy/pytest config
├─ uv.lock                       # single lock for all services
├─ .env.example  .gitignore  .dockerignore  .editorconfig  .secrets.baseline
├─ docker-compose.yml            # full stack
├─ docker-compose.ci.yml         # CI variant (no model key, deterministic provider)
├─ .github/workflows/ci.yml      # lint → typecheck → unit → build → integration → eval → secret scan
├─ docs/
│  ├─ README.md                  # docs index (start here)
│  ├─ agent-platform-assignment/ # the provided assignment — committed, never edited
│  ├─ plan/
│  │  ├─ MASTER-PLAN.md          # this file
│  │  ├─ 00-conventions.md       # working agreements, gitignore policy, commit style
│  │  └─ stage-00.md … stage-16.md
│  ├─ adr/0001..000n-*.md        # decisions + rejected alternatives
│  ├─ architecture/              # overview, data-model, tool-catalog, approval-state-machine,
│  │                             # memory, observability, failure-handling, mock-contract
│  ├─ runbooks/                  # local-dev, evals, provider-setup, troubleshoot
│  ├─ media/                     # screenshots + recording (deliverable 4)
│  ├─ internal/                  # GITIGNORED — scratch, private notes
│  └─ scratch/                   # GITIGNORED
├─ mock-ticketing/               # starter kit — UNMODIFIED
├─ services/
│  ├─ agent/                     # :8080 — AgentCore Runtime workload
│  │  ├─ src/agent_service/      # api/ config.py agent/ approval/ memory/ tools/
│  │  │                          # providers/ mcp/ db/ observability/
│  │  ├─ alembic/versions/       # migrations
│  │  ├─ tests/
│  │  ├─ alembic.ini  Dockerfile  pyproject.toml
│  ├─ mcp-ticketing/             # :8081 — FastMCP tools + ticketing client
│  │  ├─ src/ticketing_mcp/      # server.py client.py errors.py retry.py
│  │  │                          # idempotency.py verify.py config.py
│  │  ├─ tests/  Dockerfile  pyproject.toml
│  └─ web/                       # :3000 — thin UI
│     ├─ static/                 # index.html, css/, js/{api,state,render,trace}.js
│     ├─ nginx.conf  Dockerfile
├─ eval/
│  ├─ cases.json  runner.py  assertions.py
│  └─ results/latest.md  latest.json
├─ tests/                        # cross-service integration tests only
└─ data/                         # GITIGNORED — sqlite + JSONL traces (runtime)
```

---

## 4. How we work

- **One stage at a time.** At the start of a stage I write `docs/plan/stage-NN.md` (goal, files, DoD,
  estimate). At the end I mark it done, report the DoD result and the tick, and stop for your review.
- **A stage is only done when its Verify command passes on a clean checkout.** No stage is declared
  done on "it should work".
- **No claim without evidence.** Every README claim maps to a test, an eval case or a command output.
- **Secrets:** never committed. `.env` is gitignored; a secret scan runs in CI. Keys are never
  logged, traced, stored in the DB or returned by any endpoint — enforced by a test in Stage 7.
- **Progress** is tracked in the task checklist and in each `stage-NN.md`.

---

## 5. Stages, deliverables and estimates

Estimates are given twice: **Agent** = active working time for me to produce and verify the stage;
**Human** = a senior engineer doing the same work with normal tooling. Agent time is *not* human
time — the human column is the honest "what would this cost a team" figure for the assignment.

### Stage 0 — Repo bootstrap, conventions, this plan
- **Deliverables:** git init; full directory tree; `pyproject.toml` (uv workspace + ruff + mypy +
  pytest); `uv.lock`; `Makefile`; `.gitignore` (incl. `docs/internal/`, `docs/scratch/`, `data/`,
  `.env*` except example); `.editorconfig`; `.dockerignore`; `.env.example`; `LICENSE`;
  `docs/README.md`; `docs/plan/00-conventions.md`; `docs/adr/0001-monorepo-and-service-boundaries.md`,
  `0002-agent-orchestration-and-providers.md`, `0003-python-dependency-management.md`;
  archive the kit README to `docs/starter-kit/README.md`; move `mock-ticketing/` to the root.
- **DoD:** `make help` lists targets; `uv sync` succeeds; `ruff` and `mypy` clean; the mock is
  byte-identical to the kit (`git diff --stat` proves it); one meaningful first commit.
- **Verify:** `make lint typecheck` → clean; `git status` → clean tree.
- **Estimate:** Agent 1–1.5 h · Human 0.5 day

### Stage 1 — Mock verified, not assumed
- **Deliverables:** `tests/integration/test_mock_contract.py` pinning *actual* behaviour — pagination
  shape and `has_more`, uppercase ticket ids, `PATCH` requiring ≥1 field, 401 body shape, enum
  rejection on `POST /tickets`, `FLAKY_SEED` determinism, `POST /admin/reset`; plus
  `docs/architecture/mock-contract.md` recording the two quirks that shape our design:
  (a) chaos middleware fails **before** the handler, so a 500 can never occur after a write;
  (b) `LATENCY_MS` is charged before routing, so a client timeout does **not** cancel the write —
  this is exactly why read-back verification exists.
- **DoD:** suite green against the running container; quirks documented with the curl that proves each.
- **Verify:** `make test-integration -k mock`
- **Estimate:** Agent 0.5–1 h · Human 0.5 day

### Stage 2 — Database layer and Alembic baseline
- **Deliverables:** SQLAlchemy 2.0 models — `sessions`, `messages`, `approvals`, `user_facts`,
  `idempotency_keys`, `traces`, `settings`; a `settings` table for the selected provider/model;
  `repositories/` with the user-scoped query surface (every read carries `user_id`);
  `alembic.ini`, `alembic/versions/0001_init.py`; container-start migration guard; optional
  `postgres` compose profile we actually run once.
- **DoD:** `alembic upgrade head` → `downgrade base` → `upgrade head` round-trips clean; inspector
  test asserts NOT NULLs, the `user_id` index and the unique constraint on
  `idempotency_keys(client_request_id)`; Postgres smoke run passes.
- **Verify:** `make test-unit -k migration`; `docker compose --profile postgres up -d && make migrate`
- **Estimate:** Agent 1.5–2 h · Human 1–1.5 days

### Stage 3 — MCP transport spike (de-risk first)
- **Deliverables:** `services/mcp-ticketing/` FastMCP server with `MCP_TRANSPORT=stdio|http`, a
  `/healthz` (HTTP mode), `X-MCP-Token` auth on the HTTP transport, Dockerfile; an agent-side MCP
  client that performs `initialize` → `tools/list` → `tools/call` over **both** transports.
- **DoD:** one passing test per transport (list + call a stub tool); documented fallback if the SDK's
  streamable-HTTP client path misbehaves (drop to stdio in-container and say so).
- **Verify:** `make test-integration -k mcp_transport`
- **Estimate:** Agent 1.5–2.5 h · Human 0.5–1 day

### Stage 4 — Ticketing client: errors, retries, idempotency, verification
- **Deliverables:** `ticketing_client.py` (httpx, `X-API-Key`, per-call timeout + overall deadline);
  `errors.py` (taxonomy → model-actionable text: `not_found`, `validation`, `unauthorized`,
  `rate_limited`, `server_error`, `timeout`, `transient_exhausted`); `retry.py` (exponential backoff
  + jitter, honour `Retry-After`, **never** retry 401/404/422); `idempotency.py` (SQLite store keyed
  on `client_request_id`); `verify.py` (read-back after a write timeout before any retry).
- **DoD:** table-driven tests over every status path; `Retry-After` honoured to the second; retries
  counted honestly and surfaced as `attempts`/`retried`; a simulated write-timeout proves read-back
  prevents a second POST.
- **Verify:** `make test-unit`
- **Estimate:** Agent 2–3 h · Human 1.5–2 days

### Stage 5 — The four MCP tools
- **Deliverables:** `search_tickets` (paginated: `total`, `has_more`, explicit page hint),
  `get_ticket`, `create_ticket`, `add_comment`; enums inline in schemas; errors returned as
  `isError: true` with a structured actionable payload; descriptions written for *tool selection*
  (when to use / when **not** to use, and what to do on failure); `docs/architecture/tool-catalog.md`
  with the full name/schema/description/error table.
- **DoD:** integration tests green for page 2 of a result set, not-found hint, invalid enum,
  unauthorised, flaky search recovering with `attempts ≥ 2`, flaky write producing **exactly one**
  ticket, and pagination hint present when `has_more` is true.
- **Verify:** `make test-integration -k tools`
- **Estimate:** Agent 1.5–2 h · Human 1–1.5 days

### Stage 6 — Agent service skeleton (AgentCore contract)
- **Deliverables:** FastAPI app on `0.0.0.0:8080`; `GET /ping` → `{"status":"Healthy"|"HealthyBusy"}`;
  `POST /invocations` returning `{output, tool_calls, pending_approval}`; session id from
  `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`; `user_id` resolution (body → `X-User-Id` → default);
  request id + `X-Request-Id` response header; validation error envelope; canned reply until Stage 7.
- **DoD:** the `CONTRACT.md` smoke curl passes **verbatim**; response-shape test asserts required
  fields and types; `/ping` flips to `HealthyBusy` during an in-flight background operation.
- **Verify:** the `curl` block copy-pasted from `CONTRACT.md`
- **Estimate:** Agent 1–1.5 h · Human 0.5–1 day

### Stage 7 — Provider abstraction, multi-provider selection, secret hygiene
- **Deliverables:** `ModelProvider` protocol + normalised types (`ChatMessage`, `ToolCall`,
  `AssistantTurn`, `Usage`); adapters `anthropic`, `openai`, `openai_compatible`, `bedrock`,
  `scripted`, `replay` (record a live run → replay deterministically in CI); config-driven registry;
  `GET /api/providers` → `{id, label, configured, models}` — **never** a key or a key fingerprint;
  `PUT /api/settings/model` to switch provider per session (persisted in `settings`);
  `ALLOW_RUNTIME_KEY_ENTRY=1` opt-in dev mode holding a key **in memory only**;
  `docs/runbooks/provider-setup.md`; optional `local-llm` compose profile (Ollama).
- **DoD:** fixture-based adapter unit tests pass; `@pytest.mark.live` smokes skip without a key;
  a **secrets-leak test** proves a configured key appears in no log line, trace field, DB row or HTTP
  response; provider switch takes effect on the next `/invocations` without a restart.
- **Hardware note (measured, not assumed):** GTX 1050 Ti / 4 GB VRAM, 16 GB RAM, no GPU passthrough
  into Docker on Windows → local models are CPU-only and limited to ~3B-class, slow and weak at tool
  selection. Documented as "swappability demo", never as the demo path.
- **Verify:** `make test-unit -k provider and secrets`
- **Estimate:** Agent 3–4 h · Human 2–3 days

### Stage 8 — Agent loop and the structural approval gate
- **Deliverables:** loop with step/time/token budgets; MCP tool discovery at startup so the agent
  never hardcodes schemas; `tools/registry.py` with `requires_approval`; `approval/propose.py`
  (builds the exact call + `client_request_id`), `approval/decide.py` (deterministic, unit-tested
  approve/decline classifier — the model is **not** asked "is this approval?"), `approval/execute.py`
  (the only path that can invoke a write tool); one-pending-approval-per-session rule; 30-min TTL;
  `docs/architecture/approval-state-machine.md`.
- **DoD:** tests prove — a write proposed by the model is intercepted and never executed during the
  loop; approve executes exactly once; decline writes nothing; a second proposal while one is pending
  is rejected with a clarifying tool error; a digression keeps the approval alive and re-surfaces it;
  an expired approval refuses to execute; a **restart between propose and approve** still executes
  exactly once.
- **Verify:** `make test-unit -k approval`
- **Estimate:** Agent 3–4 h · Human 2–2.5 days

### Stage 9 — Memory
- **Deliverables:** session history windowing + running summary; `user_facts` with a hard key
  whitelist (`requester_name`, `team`, `preferred_category`), deterministic extractor (regex for
  "I'm Priya Raman") plus an optional model extractor, TTL (180 days), prompt injection as defaults,
  `DELETE /api/memory/{user_id}`; `docs/architecture/memory.md` (what / where / how long / for whom).
- **DoD:** tests prove — a fact learned in session A is used in session B for the same user; a second
  user's prompt and response contain none of the first user's facts; the whitelist rejects free text
  and ticket bodies; an expired fact is not injected; a session survives a restart.
- **Verify:** `make test-unit -k memory`
- **Estimate:** Agent 2–3 h · Human 1.5–2 days

### Stage 10 — Observability
- **Deliverables:** span model (request → `model_call` / `tool_call` / `approval` / `error`);
  one JSONL file per request; SQLite index; `GET /api/traces/{request_id}` and
  `GET /api/traces?user_id=`; redaction rules; honest `tool_calls[]` in the `/invocations` response;
  optional OTLP export behind a flag; `docs/architecture/observability.md`.
- **DoD:** every request yields a trace containing model and tool spans with latency, tokens, attempt
  counts and errors; a retried call shows every attempt; no secret or raw API key appears anywhere;
  a deliberately bad run (Stage 12 fault) is fully explainable from its trace alone.
- **Verify:** `make test-unit -k tracing`, then `curl /api/traces/<id>` on a real run
- **Estimate:** Agent 2–3 h · Human 1.5–2 days

### Stage 11 — Web UI
- **Deliverables:** `services/web` (nginx, port 3000) proxying `/api/*` → agent:8080, serving static
  ES modules: transcript; tool-call table (name, args, status, attempts, duration); approve/decline
  card driven by `pending_approval`; provider/model selector with configured badges; memory panel
  with forget; per-request trace panel; two demo users to show isolation; cancel button placeholder.
- **DoD:** real browser walkthrough of read → propose → approve → decline → digression; layout holds
  at 375 px and 1440 px with no overflow; labelled controls and visible focus; no emoji.
- **Verify:** `docker compose up` then a manual pass (recorded for deliverable 4)
- **Estimate:** Agent 3–4 h · Human 2 days

### Stage 12 — Failure hardening and request cancellation (the one optional extra)
- **Deliverables:** one global deadline budget honoured across loop and MCP; `transient_exhausted`
  turned into an honest user-facing message; cooperative cancel tokens threaded through the loop,
  MCP calls and write guard; `POST /invocations/{request_id}/cancel`; Cancel button wiring;
  `HealthyBusy` while background work runs; `docs/architecture/failure-handling.md`.
- **DoD:** a cancelled run performs **no write** even if cancellation lands mid-tool-call; a
  rate-limit storm across propose+approve still yields exactly one ticket; a timeout run tells the
  user it gave up and does not retry forever; every one of these is replayable from its trace.
- **Verify:** `make test-integration -k failure`
- **Estimate:** Agent 2–3 h · Human 1.5–2 days

### Stage 13 — Evals
- **Deliverables:** `eval/cases.json` — the 9 provided samples mapped to our tool names, plus our own:
  restart-between-propose-and-approve, approval expiry, memory across sessions, memory isolation
  between users, pagination page 2, not-found clarification, invalid enum handled, flaky-write
  idempotency (exactly one ticket), out-of-scope refusal, digression then resume, timeout gives up,
  secret-never-leaked; `eval/runner.py` driving the **real running service over HTTP** with mock reset
  and per-case mock env; `eval/assertions.py` (tool_called, no_write_before_approval, ticket_created,
  no_duplicate_tickets, comment_added_to, output_contains_any, declines, completes, retries_observed,
  latency budget); `results/latest.{md,json}`; non-zero exit on failure; ADR-0005.
- **DoD:** `make eval` green on the deterministic provider; a deliberately broken case is proven to
  fail the run (then reverted); one live-model run recorded and committed; results table in the README.
- **Verify:** `make eval`
- **Estimate:** Agent 3–4 h · Human 2 days

### Stage 14 — Deployment polish and CI
- **Deliverables:** multi-stage Dockerfiles, non-root users, healthchecks on all four services,
  `--platform linux/arm64` build support documented (tested amd64 locally — stated per `CONTRACT.md`),
  `.dockerignore`s, complete `docker-compose.yml` + `docker-compose.ci.yml`, `Makefile` targets,
  `.github/workflows/ci.yml` (lint → typecheck → unit → build → integration → eval → `gitleaks`),
  build-cache layer ordering with `uv sync --frozen`.
- **DoD:** `docker compose up -d` brings all four services to healthy with a single command; `make ci`
  reproduces the pipeline locally; an ARM64 build succeeds via buildx (or the limitation is stated).
- **Verify:** `docker compose up -d && docker compose ps`; `make ci`
- **Estimate:** Agent 2–3 h · Human 1–1.5 days

### Stage 15 — Documentation: README a–k and AGENTCORE.md
- **Deliverables:** `README.md` answering **every** heading a–k (quick setup, architecture + diagram,
  agent design incl. frameworks/models considered and rejected, state and approval mechanics, memory
  choices, tool naming/description/error design, MCP→mock authentication, eval cases + latest results
  + what evals don't catch + guardrails, observability and how a trace debugs a bad run, key decisions,
  engineering standards followed *and* skipped, how AI tools were used, what I'd do differently);
  `AGENTCORE.md` written from the current AWS docs with a diagram, what I'd keep/change, where it gets
  hard, and an explicit "what I'd verify before relying on it" list; `docs/media/` screenshots +
  recording; `docs/runbooks/troubleshoot.md`.
- **DoD:** every a–k heading present and specific; the AgentCore claims are traceable to AWS docs;
  the run recording shows a read, a proposed write, an approval, and a failure handled; the eval
  output is pasted from a real run.
- **Verify:** fresh-clone read-through + the recorded run
- **Estimate:** Agent 3–4 h · Human 2 days

### Stage 16 — Clean-slate verification and submission
- **Deliverables:** clone into a temp directory → `make up` → `make eval` → browser walkthrough to
  prove nothing depends on my machine; tag `v1.0.0`; push to `serhiiliutov/agentcore-platform`
  (switch the active `gh` account — currently `AIDataMaster`).
- **DoD:** a fresh clone reproduces every result; the public repo is complete, with no secret in
  history (`gitleaks` over full history).
- **Verify:** the clone run, start to finish, recorded
- **Estimate:** Agent 1–1.5 h · Human 0.5 day

---

## 6. Totals

| | Agent (active) | Human (senior) |
|---|---|---|
| Stages 0–5 (foundation: repo, mock, DB, MCP, tools) | 8–12 h | 5–7 days |
| Stages 6–10 (agent core: contract, providers, approval, memory, tracing) | 11–16 h | 8–11 days |
| Stages 11–13 (UI, failure handling, evals) | 8–11 h | 5.5–7.5 days |
| Stages 14–16 (deployment, docs, verification) | 6–8.5 h | 3.5–5 days |
| **Total** | **≈ 33–47 h active** | **≈ 22–30 days** |

### Minimum viable submission (~5–7 human days)
Stages 0, 1, 3, 4 (reduced), 5, 6, 7 (scripted + one real provider), 8, 9 (session facts only), 13
(core cases), plus a short README and AGENTCORE.md. Sacrificed: cancellation, replay mode, Postgres
profile, OTLP, local-LLM profile, the elaborate UI.
**We are not doing this** — the assignment states a solid, well-engineered solution is valued over a
complex one, but it also asks for memory, evals, traces and docs as scored criteria, so the full plan
above is the right target with the optional items kept genuinely optional.

---

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| MCP SDK streamable-HTTP client is awkward (biggest unknown) | Stage 3 front-loads it, before any agent logic depends on it; stdio fallback documented |
| Model tool-selection quality varies by provider | Deterministic `scripted`/`replay` providers keep evals stable; live runs are labelled with their provider; tool descriptions are written for selection and tested |
| The mock cannot express "500 after a successful write" | Recognised in Stage 1; the realistic timeout-after-write case is covered by read-back verification; gap stated in the README |
| Approval semantics ambiguous when the user digresses | One pending approval per session; deterministic classifier; unrecognised input keeps the approval alive |
| Scope drift (key entry, local models, extra UI) | Locked in this plan; local-LLM and runtime key entry are opt-in, off by default, and never on the demo path |
| Windows/Docker quirks (line endings, ARM64, volumes) | `.gitattributes`, `.editorconfig`, healthchecks, and a clean-clone verification stage |
| `gh` account mismatch (AIDataMaster vs serhiiliutov) | Resolved at Stage 16 before the first push; nothing is published until it is |

---

## 8. Definition of done for the whole assignment

1. `git clone` → `cp .env.example .env` → `make up` → all four services healthy, one command.
2. `make ci` green: lint, type-check, unit, integration, eval.
3. `make eval` prints a pass/fail table, writes `eval/results/latest.{md,json}`, exits non-zero on any
   failure, and one case is proven able to fail the run.
4. A recorded end-to-end run shows a read, a proposed write, an approval, a decline, and a failure
   being handled — and every one of those is explainable from its trace.
5. `README.md` answers a–k; `AGENTCORE.md` carries a diagram, the keep/change list, the difficult
   parts and the "verify before relying on it" list.
6. No secret in the working tree or in git history. Public repo tagged `v1.0.0`.
