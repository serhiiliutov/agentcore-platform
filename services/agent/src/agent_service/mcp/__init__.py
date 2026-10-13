"""The agent's only route to the ticketing tools.

Every tool the agent can call comes through here. That is an architectural claim, not a
convenience: ADR-0001 puts the ticketing API behind an MCP server precisely so the agent
cannot be tempted to reach the mock directly, which would bypass the idempotency ledger in
Stage 4 and make a duplicate ticket possible again.
"""

from __future__ import annotations

from .client import (
    McpClientError,
    McpConnectionError,
    McpTool,
    TicketingMcpClient,
    ToolCallResult,
    UnknownToolError,
)

__all__ = [
    "McpClientError",
    "McpConnectionError",
    "McpTool",
    "TicketingMcpClient",
    "ToolCallResult",
    "UnknownToolError",
]
