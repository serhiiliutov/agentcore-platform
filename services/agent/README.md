# agent-service

The IT helpdesk agent. It speaks the AgentCore Runtime contract, so the same
image runs locally and, in principle, on Amazon Bedrock AgentCore Runtime
unchanged.

```
POST /invocations   {"prompt": "...", "user_id": "..."}  →  {output, tool_calls, pending_approval}
GET  /ping          {"status": "Healthy" | "HealthyBusy"}
```

Plus a small `/api/*` surface used only by `services/web` (providers, memory,
traces, settings). It is namespaced away from the two contract routes on
purpose: `CONTRACT.md` is about `/invocations` and `/ping`, and nothing local
should blur that.

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
| `agent/` | the orchestration loop, prompt assembly, step and deadline budgets |
| `approval/` | propose → decide → execute for writes; the only code that may write |
| `providers/` | `ModelProvider` implementations (anthropic, openai, openai-compatible, bedrock, scripted, replay) |
| `mcp/` | MCP client — the only route to the ticketing tools |
| `tools/` | tool registry discovered from the MCP server, plus approval flags |
| `memory/` | session history and cross-session user facts (always user-scoped) |
| `db/` | SQLAlchemy models, repositories and the Alembic migration chain |
| `observability/` | spans, structured JSON logs, trace store |

## Configuration

Every setting comes from the environment (see `.env.example` at the repository
root) and is validated at startup by `agent_service.config.Settings`. There is
no configuration file and no default that silently points at a real service.

Credentials are read from the environment only. When `ALLOW_RUNTIME_KEY_ENTRY=1`
a key may also be supplied at runtime by the web UI, in which case it is held in
process memory, is never persisted, never logged and never echoed back.
