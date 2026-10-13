# Stage 5 — The MCP server: four tools, two transports

* **Status:** complete
* **Started:** 2026-10-13
* **Estimate:** agent 4–6 h · human 1.5 days
* **Depends on:** Stage 3 (the SDK spike), Stage 4 (the client)
* **Branch:** `main`

## Goal

Expose the ticketing API to an agent over MCP — the same four operations the mock has, with
the tool *contract* doing the work that documentation would otherwise have to do.

The contract is the point. A model decides when to call a tool and with what from its schema
and description alone, so the description is part of the implementation:

* `client_request_id` is **required** on both write tools, and the caller must reuse it. That
  is what makes re-executing an approved action safe, and it only works if the key is chosen
  *before* the approval rather than inside the tool.
* Reads return `total` and `has_more`, so a model can tell "these are all of them" from
  "these are the first ten of forty" instead of stating the wrong thing confidently.
* Failures come back as results carrying their *kind*, not as raised exceptions, so a model
  can distinguish "fix your arguments" from "that does not exist" from "wait".

## Deliverables

| Module | Responsibility |
|---|---|
| `tools.py` | The four tools as plain async functions, no MCP plumbing |
| `server.py` | `build_server`: registers tools on `MCPServer`, converts outcomes |
| `config.py` | Process settings from the environment; key as `SecretStr` |
| `__main__.py` | Transport selection; exit 2 on misconfiguration, not a traceback |

Tests: `tests/unit/test_mcp_tools.py` (22), plus the shared fake extracted to
`tests/unit/fakes.py` so both MCP suites describe the API once.

## What the SDK actually is

Pinned by running it, as in Stage 3 — not from a remembered API:

| Fact | Consequence |
|---|---|
| `MCPServer` (not `FastMCP`) from `mcp.server.mcpserver` | the class Stage 3 found |
| `@server.tool(name=..., description=...)` | registration, schema from the signature |
| `run(transport='stdio' \| 'streamable-http')` | **one** entry point for both |
| Tools return `CallToolResult(content, structured_content, is_error)` | a sentence and JSON |
| `Tool.input_schema` | **snake_case**, not `inputSchema` |
| `middleware=` on the constructor | available to Stage 10 for tracing |

`run` covering both transports is what makes ADR-0001's "both transports, one codebase" real:
the tools are defined once and neither transport knows about the other.

## Definition of done

| Check | Command | Result |
|---|---|---|
| Lint is clean | `python -m devtasks lint` | **pass** — `All checks passed!` |
| Formatting is clean | `python -m devtasks fmt` | **pass** — no diff |
| Types are clean | `python -m devtasks typecheck` | **pass** — `Success: no issues found in 43 source files` |
| Unit tests | `python -m devtasks test-unit` | **pass** — `154 passed` |
| Exactly four tools are registered | `test_the_server_registers_exactly_the_four_expected_tools` | **pass** |
| `client_request_id` is required *in the schema* | `test_the_write_tools_publish_client_request_id_as_required` | **pass** |
| Reads do not require a key | `test_the_read_tools_do_not_require_a_key` | **pass** |
| Both transports are reachable | `__main__ --transport {stdio,streamable-http}` | **pass** — one `run` call each |

## Findings worth recording

* **`input_schema`, not `inputSchema`.** Two tests failed on this before I read the attribute
  rather than assuming it. The MCP types are pydantic models with snake_case fields; the
  camelCase names in the MCP *wire format* are not the Python attribute names. A test that
  trusts the wire spelling fails for a reason that looks like the SDK being wrong.
* **The schema is a real contract, not a mirror of the function.** Asserting on
  `input_schema["required"]` rather than on the Python signature is deliberate: a tool whose
  function accepts the key but whose published schema does not *require* it would let a model
  omit it, and the approval guarantee would be unreachable even though every unit test on the
  function itself passed.
* **`build_server` had to be re-typed to the protocol.** It originally took the concrete
  `TicketingClient`, which meant the scripted transport could not be injected without a cast.
  Same reasoning as Stage 4's writer — depend on the behaviour, and keep the real class
  checked against it.
* **Failing at startup beats failing on the first call.** A server that starts without an API
  key looks healthy to a supervisor and broken to a user. `ServerSettings.from_env` refuses,
  and `__main__` exits 2 with the reason rather than a traceback.

## Result

Four tools, both transports, 154 tests green. The write key is required by the schema, the
tool descriptions state the semantics a model needs to use them correctly, and failures carry
enough structure for an agent to choose a sensible next action.

## Carried forward

* Stage 6's MCP client discovers the tool surface with `list_tools()` instead of hardcoding
  it, which is the anti-drift property ADR-0001 claims.
* The approval gate is still **not** here, and deliberately so. This server performs writes
  when asked; deciding whether a write *should* happen is the agent's state machine, in
  Stage 8. The instructions block says so explicitly, so a model does not go looking for a
  confirmation step this layer does not provide.
* Stage 10 can attach tracing through the server's `middleware` parameter rather than
  wrapping every tool by hand.
