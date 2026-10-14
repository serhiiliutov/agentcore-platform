# Architecture overview

Three processes, one request path, and one decision that everything else is arranged around: **a
write cannot happen without a human saying yes, and that yes survives a restart.** This document is
the orientation — the components, the path a request takes, and where to read next.

## The shape

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
                 │  mock-ticketing       :8000│  unmodified starter-kit fixture
                 │  flaky on demand           │  tickets · comments · fault injection
                 └────────────────────────────┘
```

Three properties fall out of that shape, and they are the reason for it:

- **A write cannot happen without a human.** Write tools are intercepted before execution and become
  a pending approval. Only an explicit approval executes the call, and the pending state is in the
  database, so it survives a restart. → [`approval-state-machine.md`](approval-state-machine.md)
- **A failure is described, not swallowed.** Rate limits, 500s and lost responses are handled inside
  the MCP server and reported to the model as structured, actionable errors. →
  [`failure-handling.md`](failure-handling.md), [`tool-catalog.md`](tool-catalog.md)
- **Any run can be explained afterwards.** Every turn, tool call, retry, approval and provider call
  is a span in a trace you can read. → [`observability.md`](observability.md)

## The three components

| Component | Owns | Does not |
|---|---|---|
| **agent** (`services/agent/`) | the model loop, the approval gate, memory, traces, the page, the HTTP contract | touch the ticketing system; hold a ticketing credential; know that HTTP is what the tools speak |
| **mcp-ticketing** (`services/mcp-ticketing/`) | the tool surface, the credential to the mock, retry policy, the error taxonomy, the write ledger | decide whether a write is allowed; know what a conversation is |
| **mock-ticketing** (`mock-ticketing/`) | tickets, comments, and failing on demand | anything else. It is the starter kit's fixture, unmodified and checksum-verified. → [`the-mock.md`](the-mock.md) |

The boundary between the first two is the one that matters. The agent reaches the ticketing system
**only** through the MCP server, which is the only component that holds the API key — so the agent
cannot leak a credential it never has. The MCP server is also where "which failures are worth
retrying" lives, because that answer depends on the backend rather than on the conversation.

## Inside the agent

The agent is one FastAPI application whose modules each own one thing. In the order a request meets
them:

| Module | Responsibility |
|---|---|
| `api/app.py` | the contract: `POST /invocations`, `GET /ping`, the session header, error mapping |
| `api/models.py` | the request and response shapes, including `pending_approval` and `stop_reason` |
| `api/approval_routes.py`, `api/cancel_routes.py` | deciding a proposal; stopping a running turn |
| `agent/handler.py` | one turn: load history, run the loop, translate the result to the wire |
| `agent/loop.py` | the loop itself — model turn, tool calls, checkpoints, proposal-on-write |
| `agent/gate.py`, `agent/execution.py` | the approval gate and the executor that performs an approved write |
| `agent/memory.py` | extracting a fact from a prompt, injecting facts into the system prompt |
| `agent/budget.py`, `agent/inflight.py` | the turn deadline, the cancel token, the registry of running turns |
| `tools/registry.py` | which tools exist and which need approval (derived from their schemas) |
| `mcp/client.py` | the MCP session, tool discovery, and calling a tool |
| `providers/` | the model providers behind one interface, plus the deterministic `scripted` one |
| `observability/` | the span model and the writer for spans + the summary row |
| `db/` | SQLAlchemy models and repositories — the only place that writes state |
| `ui/` | the page, served as a constant at `/` |

Two conventions hold across all of them: **state lives in `db/` and nowhere else** (so a restart
loses nothing that matters), and **anything user-scoped takes a `user_id`** (so `user_id` is never a
filter somebody remembered to add). → [`data-model.md`](data-model.md), [`memory.md`](memory.md)

## What happens in one turn

The sequence for *"the printer is jammed, raise a ticket"*:

1. `POST /invocations` with `prompt`, `user_id`, and a session id (body or
   `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`). `/ping` reports `HealthyBusy` while this runs.
2. The turn is registered in the in-flight registry, given a deadline, and a trace opened.
3. Facts about the user are loaded and injected into the system prompt (per turn, nothing written
   back to the transcript).
4. The loop calls the provider with the tool catalogue the MCP server published.
5. **The model asks for `create_ticket`.** The registry says that tool requires approval, so the loop
   *records a proposal* — the tool name and the exact arguments — and ends the turn. It does not call
   anything.
6. The response carries `stop_reason: "awaiting_approval"` and the `pending_approval`. The page shows
   a card with the arguments and an expiry.
7. `POST /invocations/approvals/{id}/decide`. The gate records the decision, then — only on approval
   — executes the stored arguments through the MCP server, which calls the mock with the idempotency
   key minted back at step 5.
8. The trace holds every step, and the `traces` row makes it findable.

A *read* is the same sequence with step 5 executing immediately: `search_tickets` is not a write, so
it runs, its result goes back to the model, and the answer is composed from it. Which tools are reads
is derived from their schemas, not from a list. → [`tool-catalog.md`](tool-catalog.md)

## Health, ports, and running it

| Service | Port | Health |
|---|---|---|
| agent | 8080 | `GET /ping` → `{"status":"Healthy"}`, or `HealthyBusy` while a turn is in flight |
| mcp-ticketing | 8081 | its own healthcheck, and the MCP handshake |
| mock-ticketing | 8000 | `GET /health` (unauthenticated) |

One command starts all three with healthchecks enforced: `docker compose up -d --wait` (or
`make up-ci` for the deterministic CI settings). Both images cross-build for `linux/arm64`, which is
what AgentCore Runtime expects; the *run* on ARM64 is not claimed, and the reason is stated in
[`plan/stage-14.md`](../plan/stage-14.md).

## Reading order

1. This document.
2. [`architecture/overview`](#inside-the-agent) → the module table above, then the source.
3. [`tool-catalog.md`](tool-catalog.md) — the tools, their descriptions, and the error taxonomy.
4. [`approval-state-machine.md`](approval-state-machine.md) — the guarantee.
5. [`data-model.md`](data-model.md) and [`memory.md`](memory.md) — what is stored, and for whom.
6. [`failure-handling.md`](failure-handling.md) — retries, the deadline, and giving up honestly.
7. [`observability.md`](observability.md) — how to explain a run afterwards.
8. [`the-mock.md`](the-mock.md) — the thing all of this exists to survive.
9. [`AGENTCORE.md`](../../AGENTCORE.md) — how it would run on AWS, and what I would verify first.

## What this architecture does not include

- **No queue and no background worker.** A turn is handled inside its own request. The consequence is
  that a restart kills turns in flight — which is why cancellation is cooperative and why the
  in-flight registry is deliberately process-local.
- **No second agent and no handoff.** The optional extra was cancellation, chosen because the
  approval workflow makes "waiting" the normal state and a user who can stop a turn is the more
  useful half of the pair. Streaming was not built; the page shows the completed turn.
- **No shared memory between users.** Two people on one team keep separate facts, deliberately. →
  [`memory.md`](memory.md)
