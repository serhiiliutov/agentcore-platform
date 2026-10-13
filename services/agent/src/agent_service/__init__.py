"""agent-service — the IT helpdesk agent, exposed over the AgentCore Runtime contract.

Layout
------
``api/``            the HTTP surface: ``POST /invocations``, ``GET /ping`` and the
                    ``/api/*`` convenience endpoints the web UI uses.
``agent/``          the orchestration loop: prompt assembly, step budget, dispatch.
``approval/``       propose → decide → execute. The write path lives here and nowhere else.
``providers/``      the swappable model providers behind one ``ModelProvider`` protocol.
``mcp/``            the MCP client. The agent reaches the ticketing mock only through it.
``memory/``         session history and cross-session user facts, always user-scoped.
``db/``             SQLAlchemy models and repositories.
``tools/``          the tool registry: which tools exist and which need approval.
``observability/``  spans, structured logs and the trace store.

Design rules that hold everywhere in this package
-------------------------------------------------
1. A model can never write on its own. Write tools are intercepted by the tool
   registry and turned into a pending approval; only ``approval.execute`` performs
   the call, and only for an approval that was explicitly approved.
2. Every read of user data is scoped by ``user_id`` in a single repository layer.
   There is no code path that queries facts or messages without it.
3. Nothing that reaches a log, a trace, a database row or an HTTP response may
   contain a credential. Enforced by ``tests/unit/test_secret_hygiene.py``.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
