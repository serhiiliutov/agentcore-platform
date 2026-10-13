"""ticketing-mcp — the MCP server that stands between the agent and the ticketing mock.

Layout
------
``config.py``        environment-driven settings (transport, retry policy, credentials).
``client.py``        the HTTP client for the mock: auth, timeouts, deadline budget.
``errors.py``        the error taxonomy and the model-actionable messages it produces.
``retry.py``         backoff, jitter and the retry/non-retry decisions.
``idempotency.py``   the ledger that makes a retried write return the first result.
``verify.py``        read-back verification after a write whose response was lost.
``tools.py``         the four tool definitions the model actually sees.
``server.py``        FastMCP wiring and the transport (stdio or streamable HTTP).

Why this service exists at all
------------------------------
The agent is not allowed to talk to the mock directly. Everything it knows about
tickets arrives as a tool result from here, which means this is the only place
that has to be good at being let down by an unreliable backend: rate limits,
500s, slow responses and lost responses are all handled here, and the agent sees
a clean, described outcome either way. ``X-API-Key`` never leaves this process.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
