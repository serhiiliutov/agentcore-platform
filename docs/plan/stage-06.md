# Stage 6 — The agent service skeleton and its MCP client

* **Status:** complete
* **Started:** 2026-10-13
* **Estimate:** agent 5–7 h · human 2 days
* **Depends on:** Stage 2 (settings, schema), Stage 5 (the MCP server)
* **Branch:** `main`

## Goal

Stand up the agent service far enough that the platform can call it and it can reach the
tools — and no further. The orchestration loop is Stage 8; this stage builds the two surfaces
it will sit between, so that stage is a dependency injection rather than a rewrite.

## Deliverables

| Module | Responsibility |
|---|---|
| `mcp/client.py` | The only route to the tools: connect, discover, call |
| `api/models.py` | The AgentCore contract's request and response shapes |
| `api/app.py` | `create_app`: `/ping`, `/invocations`, injectable handler |
| `config.py` (extended) | `mcp_stdio_command`, `mcp_expected_server_name` |
| `tests/contract/test_agent_contract.py` | 12 tests over the two contract routes |

## Three decisions that are decisions

**The tool surface is discovered, not hardcoded.** `connect` calls `initialize` then
`list_tools`. A tool added to the server appears in the agent with no change here; a tool
removed cannot be called. Four hardcoded names would have been simpler and would have quietly
thrown away the property ADR-0001 cites as the reason for the MCP boundary at all.

**Whether a tool is a write is derived from its schema.** A write is a tool that *requires*
`client_request_id` — the structural signal Stage 5 publishes and tests. The alternative, a
name-prefix rule, would classify a future `create_bulk_tickets` as a read and route it around
the approval gate.

**The loop is injected, not imported.** `create_app(invocation_handler=...)`; the default
answers **501**, not 500. "Not wired yet" and "broken" are different claims and a monitor
should not confuse them. This is what lets every route, status code and error shape in this
stage be finished and tested while Stage 8 is still unwritten.

## What the SDK actually is

Pinned by running it, as in Stages 3 and 5:

| Fact | Consequence |
|---|---|
| `streamable_http_client` (snake) | not `streamablehttp_client` |
| `TransportStreams` is a plain `(ReadStream, WriteStream)` **tuple** | the same unpacking works for both transports |
| `ClientSession.call_tool(name, args, read_timeout_seconds=...)` | a per-call deadline, so no timer wrapper |
| `serverInfo.name` on the initialize result | identity can be verified, not assumed |

## Definition of done

| Check | Command | Result |
|---|---|---|
| Lint is clean | `python -m devtasks lint` | **pass** — `All checks passed!` |
| Formatting is clean | `python -m devtasks fmt` | **pass** — no diff |
| Types are clean | `python -m devtasks typecheck` | **pass** — `Success: no issues found in 49 source files` |
| Tests | `python -m devtasks test-unit` | **pass** — `166 passed` |
| `/ping` reports busy under load | `test_ping_reports_busy_while_an_invocation_is_in_flight` | **pass** |
| A request without `user_id` is refused | `test_invocations_requires_a_user_id` | **pass** — 422 |
| An uninstalled loop is 501, not 500 | `test_an_uninstalled_loop_answers_501_rather_than_500` | **pass** |
| The session header wins | `test_the_session_header_wins_over_the_body_field` | **pass** |

## Findings worth recording

* **`TransportStreams` is a two-tuple, not a record.** I was about to write
  `streams.read_stream` and stopped to check, which is the habit Stages 3 and 5 have earned.
  Both transports yield the same pair, which is why `_open_session` is the only function that
  mentions the transport at all.
* **`TRY301` was right about the validation raises.** The server-name and empty-catalog
  checks were `raise` statements inside the broad `try` that converts transport errors. They
  worked — `except McpClientError: raise` re-raised them — but they were one edit away from
  being swallowed by their own handler, which is exactly the failure that convert-to-clear-
  message block exists to prevent. Extracting them made the intent structural.
* **Ruff removed an unused `noqa: BLE001`.** That told me the broad `except Exception` is
  not what the configured rule set flags here — worth knowing, because reaching for the same
  suppression twice would have been noise in the diff.
* **Resolving the session header in `app.py` rather than the loop.** The handler receives one
  object with the id already resolved, so no downstream code has to know a header exists.

## Result

The service answers the AgentCore contract, reaches the tools over either transport, and
refuses to guess: it will not call a tool it did not discover, will not run without a
`user_id`, and says plainly when the loop is not installed.

## Carried forward

* **Stage 7** implements `ModelProvider`. Nothing in this stage imports a model SDK, so the
  provider choice cannot leak into the transport or the contract.
* **Stage 8** installs the real `InvocationHandler`. It receives `InvocationRequest` and
  returns `InvocationResponse`; `app.py` does not change.
* **Stage 4's ledger and Stage 8's approval gate meet here.** The client exposes
  `write_tools` — derived, not listed — which is what the gate will consult to decide whether
  a proposed call needs a human.
