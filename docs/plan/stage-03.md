# Stage 3 — MCP transport spike: what the SDK actually is

* **Status:** complete (investigation; no product code)
* **Started:** 2026-10-13
* **Estimate:** agent 0.5 h · human 0.25 day
* **Depends on:** Stage 0
* **Branch:** `main`

## Why this stage exists

Stage 0's `uv sync` resolved `mcp` to **2.3.0** — a major version above the 1.x line
that essentially every public example and tutorial targets. Rather than write a server
against a remembered API and discover the differences through failing imports, this
stage spends half an hour on introspection first. It is deliberately scheduled **before**
any code depends on the SDK.

## What the SDK actually provides

Determined by importing and inspecting the installed package, not by reading
documentation for a different version.

### `FastMCP` does not exist any more

```
ModuleNotFoundError: No module named 'mcp.server.fastmcp'.
This is mcp 2.x, where FastMCP was renamed to MCPServer
(from mcp.server.mcpserver import MCPServer) and other APIs changed
```

The package ships that explanation inside its own import error, which is a strong
signal that this is the most common way people arrive at it.

**Consequence:** the MCP server is built on `MCPServer` from
`mcp.server.mcpserver`. Any snippet beginning `from mcp.server.fastmcp import FastMCP`
is for the previous major version.

### The client surface (what the agent will use)

| Call | Signature |
|---|---|
| `ClientSession.initialize()` | `() -> InitializeResult` |
| `ClientSession.list_tools()` | `(*, params: PaginatedRequestParams \| None = None) -> ListToolsResult` |
| `ClientSession.call_tool(...)` | `(name: str, arguments: dict[str, Any] \| None = None, read_timeout_seconds: float \| None = None, progress_callback=None, *, ...)` |

Three things worth noting for later stages:

* **`call_tool` takes `read_timeout_seconds` per call.** The agent's per-tool deadline
  can therefore be enforced at the transport rather than wrapped around it — and it
  needs a *shorter* timeout than the mock's own `LATENCY_MS` experiments to produce the
  lost-response case Stage 1 identified.
* **`list_tools()` returns a `ListToolsResult` of real schemas.** This is what lets the
  agent discover its tool surface from the server instead of hardcoding it — the
  anti-drift property claimed in ADR-0001.
* **`call_tool` can return `InputRequiredResult`.** Not used by this project, but worth
  knowing the SDK has its own notion of mid-call interaction, which is *not* the same
  thing as this project's approval gate. Ours is a product decision about writes; it
  must not be confused with, or delegated to, the SDK's protocol feature.

### Transports available

Both directions are present in the installed package:

* `mcp.server.stdio` and `mcp.server.streamable_http` — the two the MCP server offers,
  matching ADR-0001's decision to support both.
* `mcp.client.stdio` (`stdio_client`, `StdioServerParameters`) and
  `mcp.client.streamable_http` — the two the agent can use.

## Result

The spike changed the plan in one concrete way: **the MCP server is written against
`MCPServer`, not `FastMCP`.** Had this been discovered while writing Stage 5, it would
have looked like the SDK being broken rather than a renamed class.

The temporary probe script was deleted; nothing in the repository depends on it, and
these findings are the deliverable.

## Carried forward

* Stage 5 builds the tool surface on `mcp.server.mcpserver.MCPServer`.
* Stage 6's MCP client uses `ClientSession.list_tools()` for discovery and
  `call_tool(..., read_timeout_seconds=...)` for per-call deadlines.
* The AgentCore approval requirement stays in this project's own state machine. The
  SDK's `InputRequiredResult` is a different mechanism and is not a substitute.
