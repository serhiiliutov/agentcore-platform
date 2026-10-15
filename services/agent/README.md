# agent-service

The IT helpdesk agent. It speaks the AgentCore Runtime contract, so the same
image runs locally and, in principle, on Amazon Bedrock AgentCore Runtime
unchanged.

```
POST /invocations   {"prompt": "...", "user_id": "..."}  →  {output, tool_calls, pending_approval}
GET  /ping          {"status": "Healthy" | "HealthyBusy"}
```

Plus a small `/api/*` surface used only by a client of it: the chat in
`services/chat/` (which providers are configured, a user's conversations, their
outstanding approvals, their traces). It is namespaced away from the two contract
routes on purpose: `CONTRACT.md` is about `/invocations` and `/ping`, and nothing
local should blur that. **No route serves HTML** — the front end is a separate
process, and `GET /` is a 404 asserted by a test.

## Run it

From the repository root:

```bash
docker compose up agent          # needs the mcp-ticketing and ticketing services
```

Or standalone during development:

```bash
uv run uvicorn agent_service.api.app:app --reload --port 8080
```

## Test it

```bash
uv run pytest services/agent/tests -q
```

## What is in here

| Directory | Responsibility |
|---|---|
| `api/` | HTTP surface: the contract routes, request/response models, error envelope |
| `agent/` | the orchestration loop (`loop.py`), prompt assembly, budgets, the decision gate (`gate.py`, `execution.py`) and memory (`memory.py`) |
| `db/repositories/approvals.py` | propose → decide → execute for writes; the only code that may write |
| `providers/` | `ModelProvider` implementations (anthropic, openai, openai-compatible, bedrock, scripted, replay) |
| `mcp/` | MCP client — the only route to the ticketing tools |
| `tools/` | tool registry discovered from the MCP server, plus approval flags |
| `db/` | SQLAlchemy models, repositories and the Alembic migration chain |
| `observability/` | spans, structured JSON logs, trace store |

## Configuration

Every setting comes from the environment (see `.env.example` at the repository
root) and is validated at startup by `agent_service.config.Settings`. There is
no configuration file and no default that silently points at a real service.

Credentials are read from the environment only, and **no endpoint accepts a key**. The
chat may choose *which* configured provider answers and is told only whether each one
is configured — never a key, never a prefix, never a fingerprint of one. An earlier
version of this file described an opt-in path where a front end could submit a key to
be held in process memory; that path was never built, the flag was removed with the
page it belonged to, and the code that would have grown from it does not exist.
