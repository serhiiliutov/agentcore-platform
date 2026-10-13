# Stage 11 — the front end, the composition root, and running it for real

**Status: complete. Five defects found by running it; all fixed and verified end to end.**

## Why this stage was where everything broke

Stage 11 is the first stage whose deliverable is *the service running*, so it is the first that
could fail for reasons no previous stage could observe. Stages 0–10 each ended with four green
gates — formatting, linting, strict typing, and a unit suite that grew to 281 tests — and the
service still could not start. That is not a contradiction; it is what a composition root is
for. Every unit test constructed the part it was testing directly, so nothing ever exercised the
*assembly*: which environment the MCP child receives, which attribute the SDK populates, which
directory the database path resolves against, or whether two proposals can share a key.

The five defects below were all found by running the assembled service and hitting it. None of
them was reachable from a test that built its dependencies by hand, and each one is worth more
than the code it took to fix.

## Deliverables

| Piece | File | What it does |
| --- | --- | --- |
| The page | `ui/page.py`, `ui/__init__.py` | The whole front end as `INDEX_HTML`: vanilla HTML, CSS and JS, no build step and no framework. It shows each tool call and offers approve/decline on a write. |
| The listing route | `api/ui_routes.py` | `GET /` serves the page; `GET /api/approvals?user_id=…` returns what is waiting, so a proposal survives a restart. |
| The composition root | `factory.py` | Assembles engine, provider, MCP client, tool registry, handler factory, approval gate and write executor; `build_app(settings)` returns the app. |
| The entrypoint | `__main__.py` | Logging, a refusal to start when the chosen provider has no credentials, and `uvicorn.run` with the assembled app. |
| The run task | `devtasks/tasks.py` `serve` | Runs the service from `services/agent`, with the local MCP environment filled in. |

## The five defects

### 1. The MCP child never received its configuration — `4d740c7`

`mcp/client.py` built `StdioServerParameters` without an `env`. The `mcp` SDK defaults a spawned
server to a curated whitelist — `HOME`, `PATH`, `SYSTEMROOT` and so on — which is the right
default for a third-party server and wrong for this one. The ticketing server authenticates with
`TICKETING_API_KEY`, so under the default it exited 2 with `TICKETING_API_KEY is not set` before
the handshake completed, and the agent could only report `Connection closed`.

The child is now given the parent's environment in full. Deliberately not an allow-list: an
allow-list would have to be kept in step with the MCP server's own configuration, and its drift
would fail exactly as opaquely as this did.

### 2. The server-name check could never pass — `4d740c7`

`_inspect` read `serverInfo`; the SDK's pydantic model populates `server_info`. The reported name
was therefore always `''`, and the expectation check refused *every* server that has ever
existed: `expected MCP server 'ticketing' but '' answered`. Fixed by reading both, which is the
same guard `_to_tool` already carried for `inputSchema` — the same class of bug, already
documented in that file, which is why it is now guarded in both places rather than one.

### 3. A refused write was recorded as a success — `efcf432`

The defect that matters most, because it is on the assignment's central path.

An approved `create_ticket` against a healthy mock came back as `state=executed` with the detail
`ERROR: No ticket with that id exists`. A failed write was recorded as a success.

One tool result has two readers with opposite requirements. `McpToolExecutor.call` renders a
result for the loop, which needs a sentence it can show a model — failures included — so it
returns `as_model_text()`, and that prefixes a failure with `ERROR: `. The write path read the
same string, and nothing in a `str` says whether it succeeded, so `_finish` marked the row
executed. The `ERROR: ` prefix in the reported detail is what identified it: the writer was
reading its own error marker as prose.

The writer is now given a differently-shaped call rather than a more carefully parsed string.
`WriteToolExecutor.call_checked` raises `ToolCallFailedError` when the result reports `is_error`,
so only one of the two readers is handed a value it could misread. The existing `except` branch
in `execute` already turned that into a state, so the row is terminal-failed and the refusal is
reported as an outcome. This is what the module docstring already claimed and did not implement.

Verified live: the same request that returned `executed` now returns `failed`, with the mock's
ticket count unchanged — the report is true.

### 4. A reused idempotency key returned 500 — `4abc825`

`POST /invocations` failed for every proposal after the first:

```
sqlalchemy.exc.IntegrityError: UNIQUE constraint failed: approvals.client_request_id
```

`approvals.client_request_id` is globally unique, deliberately — a key names one write. But
`propose` honoured a caller-supplied key as-is, and the scripted provider supplies a fixed one
(`scripted-create-1`) on every run. The first proposal took the key; every later proposal
attempted the same insert and failed. The service could not accept a second request, which is
exactly what trying the demo twice does.

`propose` now keeps the caller's key when it is free and mints a fresh one when it is not. The
ordering matters: two proposals are two intents, and sharing a key would make the second execute
as a *replay* of the first and silently create nothing — worse than the 500, because it would
look like success. A genuine retry of one proposal still reuses its key, because that row already
exists and is not re-proposed.

Verified live: two consecutive proposals now both return `awaiting_approval`.

### 5. The MCP server was pointed at the agent, not the mock

The write failed with `No ticket with that id exists`, and the mock's ticket count never moved.

Two misdirections are worth recording, because each cost real time. First, `POST /tickets`
directly against the mock succeeded, which correctly cleared the mock. Second, the error text is
the one `_describe` produces for a `NotFoundError`, and the verify leg of `_guarded` **searches**
— so the evidence appeared to point at a stale `IN_PROGRESS` claim in the idempotency ledger.
That reading was plausible, and wrong.

Calling `create_ticket` on the MCP server with no agent involved settled it in one line:

```
HTTP Request: POST http://127.0.0.1:8080/tickets "HTTP/1.1 404 Not Found"
```

**8080 is the agent.** The server was posting ticket writes to the agent's own HTTP port, which
answers 404 for an unknown path — and a 404 on that route is reported as a missing *ticket*,
which is why the message described a ticket rather than a connection.

`config.py` held two compounding errors:

```python
ENV_BASE_URL = "TICKETING_API_URL"  # read by nothing
DEFAULT_BASE_URL = "http://127.0.0.1:8080"  # the agent's port
```

The variable name was wrong — `.env`, `.env.example` and `devtasks serve` all set
`TICKETING_BASE_URL` — so the setting was **silently ignored in every mode**, and compose was
broken identically: the MCP container would post to `127.0.0.1:8080` inside itself. The default
was then the agent's port rather than the mock's. Fixed by reading `TICKETING_BASE_URL` and
defaulting to `8000`.

This defect has the widest blast radius of the five and is the least like a coding error. It is
two names for one thing, and the running service is the only place the two are ever compared —
which is exactly why no gate could see it.

## Decisions

- **The page is served by the agent process, not a second service.** It is an ordinary client of
  `/invocations` and the decision route, so a separate container, port and CORS policy would buy
  nothing.
- **`GET /api/approvals` exists because a proposal must survive a restart.** The repository's
  `pending` is session-keyed, so a page that knows only the user could not find an outstanding
  proposal after a restart. Hence `pending_for_user`.
- **The listing reuses `PendingApproval`**, the same type the invocation response carries. Two
  shapes for one proposal would drift.
- **`create_app` gained a `lifespan` parameter** rather than assigning
  `app.router.lifespan_context` after construction.
- **The MCP connection lives in the lifespan** — not per request, which would pay a process spawn
  every turn, and not at import, which would make an unreachable server a failure to import
  rather than to serve.
- **Alembic owns the schema.** The factory deliberately does not call `create_all` (ADR 0004).
- **`devtasks serve` runs with `cwd=services/agent`** for the same reason `migrate` does: the
  default `DATABASE_URL` is a relative SQLite path, so a service started from the repository root
  would open a second, empty database and fail as `no such table`.

## What is verified, end to end

```
GET  /ping                                  {"status":"Healthy"}
GET  /                                      200, the page, with its form
GET  /api/approvals?user_id=alice           {"approvals":[]}
POST /invocations                           awaiting_approval; create_ticket proposed with its key
POST /invocations/approvals/{id}/decide     executed / failed, honestly reported
POST /invocations/approvals/{id}/decide     409 on the second attempt
startup log                                 ticketing MCP connected; 4 tool(s) available
```

Gates: `ruff format`, `ruff check`, `mypy --strict` across 82 files, 283 unit tests (281 before
this stage's two regression tests).

## Carried forward

- `factory.py` and `ui_routes.py` have no unit tests; they were verified by running the service,
  which is stronger evidence but not a regression test.
- The page has not been driven in a real browser yet; it has only been fetched and checked for
  its form.
- `[project.scripts]` has no entry for the agent service; `python -m agent_service` is the only
  documented way in, and `devtasks serve` wraps it.
- Stage 0 publish is still blocked on a credential switch: local `main` is ahead of `origin`.
