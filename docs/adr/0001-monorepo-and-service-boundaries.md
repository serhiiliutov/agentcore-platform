# ADR-0001 — One repository, four processes

* **Status:** accepted
* **Date:** 2026-10-13
* **Deciders:** Serhii Liutov
* **Related:** ADR-0003 (dependency management), ADR-0004 (persistence split)

## Context

The assignment needs an agent service, an MCP server the agent works through, a
mock system to work against, a web page, a database and an eval runner. The
starter kit provides the mock and a compose file for it, and says: *"Copy this kit
into your own repository and add your agent service, MCP server and UI next to it,
so one `docker compose up` starts everything."*

The forces in play:

* **The AgentCore contract must stay clean.** AgentCore Runtime hosts a container
  that exposes `POST /invocations` and `GET /ping` on port 8080. If the same image
  also serves the web page, the image carries UI baggage into a runtime that has
  no concept of a browser, and every local convenience route becomes something a
  reviewer has to reason about against the contract.
* **The mock must not be touched.** It is the fixture the work is judged against;
  editing it invalidates the exercise. Anything we need to survive must be
  survived *outside* it.
* **Reviewers will not spend an hour getting it running.** One command has to
  bring the whole thing up, and it has to be honest about what is running and
  whether it is healthy.
* **Reproducibility over convenience.** Separate Python projects with separate
  lock files drift; one lock file cannot drift against itself.

## Decision

One repository, four independently buildable processes, orchestrated by a single
`docker compose up`:

| Process | Port | Responsibility | May it talk to the mock? |
|---|---|---|---|
| `mock-ticketing` | 8000 | the fixture: tickets, comments, fault injection | — |
| `mcp-ticketing` | 8081 | the tool surface; retries, idempotency, error taxonomy | **yes, only it** |
| `agent` | 8080 | the AgentCore Runtime workload: model, loop, approval, memory | no |
| `web` | 3000 | the thin UI; proxies `/api/*` to the agent | no |

Rules that follow from the split:

1. **The agent has no ticketing credentials.** `X-API-Key` exists only inside the
   `mcp-ticketing` process. This is enforced by configuration, not convention: the
   agent's settings have no field for it.
2. **The agent image stays a pure AgentCore workload.** The `/api/*` routes it
   serves are namespaced away from `/invocations` and `/ping`, and the browser
   never talks to it directly — `web` proxies. Two contract routes, nothing else,
   is what a reviewer should see on the runtime image.
3. **The mock is a verbatim copy of the starter kit.** Its files are unmodified
   and this is verifiable with a diff against the kit.
4. **One `uv.lock` for all Python.** Each image installs only the packages of the
   service it runs (`uv sync --package …`), so the shared lock does not bloat a
   runtime image.

## Alternatives considered

**One service, one image.** The agent, the MCP server and the UI in a single
FastAPI app. Less to build and to run. Rejected for three reasons: the MCP server
is required to be a server the agent *reaches through*, which stops being
meaningful if it is the same process; the AgentCore contract would be diluted by
UI routes in the same app; and the failure-injection story would test the wrong
boundary, because a fault in the "backend" would be a fault in the same process as
the agent.

**MCP over stdio only.** Simpler, no port, no auth question, and it is what most
local MCP examples do. Rejected for the containerised deployment because a stdio
MCP server is a child process of the agent, so it cannot be scaled, restarted or
observed independently — and the assignment asks how the design would run on a
managed runtime where those things matter. Stdio is still supported and used by
tests, because for a single-process test it is genuinely better.

**Separate repositories per service.** Cleanest ownership boundaries in a large
organisation. Rejected here: this is one deliverable reviewed in one sitting, and
four repositories would make "clone and run it" into four clones.

## Consequences

**Positive**

* Each image has one job, so caching, health checks, logs and restart behaviour
  are all meaningful per service.
* The MCP boundary is real, so the retry/idempotency logic is testable in
  isolation against a live mock with `FLAKY=1`.
* The AgentCore mapping in `AGENTCORE.md` is honest: the agent image is the thing
  that moves to Runtime, and the MCP container is the thing Gateway replaces.

**Negative**

* Four services to build and keep healthy; a compose-level startup-order bug is
  now possible, mitigated with health-check-gated `depends_on`.
* A cross-service change (a new tool argument) touches the MCP server, the agent's
  discovered tool schema, the eval cases and the docs. Accepted: the agent
  discovers tool schemas from the MCP server at startup rather than hardcoding
  them, which removes the most common source of drift.
* Slightly more ceremony for a small system. Accepted: it is the ceremony the
  assignment is asking about, and each piece is individually small.
