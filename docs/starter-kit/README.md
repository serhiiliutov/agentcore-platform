# Agent Platform Assignment: starter kit

Everything here runs on your laptop. No AWS account, no paid services.

## What's in the box

| Path | What it is |
|---|---|
| `mock-ticketing/` | The internal IT ticketing system your agent will work against. FastAPI; API-key auth; pagination; a `FLAKY=1` mode that returns 429s and 500s. |
| `docker-compose.yml` | Runs the ticketing API on `http://localhost:8000`. |
| `CONTRACT.md` | The AgentCore Runtime contract your agent service must follow, and the request/response shape we'll use. |
| `eval/cases.sample.json` | Sample evaluation cases. Extend them. The tool names in them are placeholders; use yours. |

## Run the ticketing API

```bash
docker compose up                         # API on :8000, docs at http://localhost:8000/docs
FLAKY=1 docker compose up                 # same, with ~30% of requests failing (429 with Retry-After, or 500)
```

Without Docker: `cd mock-ticketing && pip install -r requirements.txt && uvicorn app:app --port 8000`

All `/tickets` and `/admin` calls need the header `X-API-Key: dev-key-123` (change with `API_KEY`).

```bash
curl -H "X-API-Key: dev-key-123" "http://localhost:8000/tickets?q=vpn&status=open"
curl -H "X-API-Key: dev-key-123"  http://localhost:8000/tickets/TCK-1002
curl -H "X-API-Key: dev-key-123" -X POST http://localhost:8000/admin/reset    # restore seed data
```

Endpoints: `GET /tickets` (search, paginated) · `GET /tickets/{id}` · `POST /tickets` · `PATCH /tickets/{id}` · `POST /tickets/{id}/comments` · `POST /admin/reset`. Full spec: `/openapi.json` or `mock-ticketing/openapi.json`.

Knobs: `FLAKY=1`, `FLAKY_RATE=0.3`, `FLAKY_SEED=42` (deterministic failures, useful in tests), `LATENCY_MS=400`.

## Where your code goes

Copy this kit into your own repository and add your agent service, MCP server and UI next to it, so one `docker compose up` starts everything. Don't change the ticketing API itself; if you have to, say what you changed and why in your README.
