# ticketing-mcp

The MCP server the agent works through. The agent has no HTTP client for the
ticketing API and no API key — this process is the only thing that talks to the
mock, and the only thing that knows how to survive talking to it.

Two transports, one codebase:

```bash
MCP_TRANSPORT=http   uv run python -m ticketing_mcp --port 8081   # containers
MCP_TRANSPORT=stdio  uv run python -m ticketing_mcp              # tests and local dev
```

## The tool surface

| Tool | Kind | Purpose |
|---|---|---|
| `search_tickets` | read | paginated search with filters; returns `total` and `has_more` |
| `get_ticket` | read | one ticket with its comments |
| `create_ticket` | **write** | raises a new ticket; requires approval upstream |
| `add_comment` | **write** | appends a comment; requires approval upstream |

The upstream agent enforces approval — but this server does its part too: both
write tools take a `client_request_id`, and a repeated id returns the result of
the first call instead of writing again.

## What it guarantees

* **Retries are bounded and honest.** `429` honours `Retry-After`; `500` and
  timeouts back off with jitter; `401`, `404` and `422` are never retried because
  retrying them cannot help. Attempt counts travel back to the model.
* **A lost response is not a lost write.** If a write times out, the server reads
  back to see whether it landed before deciding to retry — because the mock
  charges `LATENCY_MS` before routing, so a client timeout does not cancel the
  handler.
* **Errors are written for a model, not a log file.** Each failure becomes a
  structured payload with a code, a plain-language message and a hint about what
  to do next. Failures that are the caller's fault say so; failures that are the
  backend's fault say not to retry.

## Authentication

* Inwards: the agent must present `X-MCP-Token` on the HTTP transport.
* Outwards: `X-API-Key` is added here and never leaves this process.

## Configuration

`MCP_TRANSPORT`, `MCP_TOKEN`, `TICKETING_BASE_URL`, `TICKETING_API_KEY`,
`TICKETING_TIMEOUT_S`, `TICKETING_DEADLINE_S`, `TICKETING_MAX_ATTEMPTS`,
`TICKETING_BACKOFF_BASE_S`, `TICKETING_BACKOFF_MAX_S`, `MCP_DB_PATH`.
See `.env.example` at the repository root.
