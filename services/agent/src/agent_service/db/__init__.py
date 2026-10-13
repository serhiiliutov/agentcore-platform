"""The agent's persistence layer: engine, schema and repositories.

Split three ways, and the split is load-bearing:

``base.py``            the engine, the session factory and the declarative base.
``models/``            the schema, one module per aggregate.
``repositories/``      every query the application is allowed to make.

The rule that makes this more than tidiness: **application code does not query the
session directly.** It goes through a repository, and every repository read of
user-owned data takes a ``user_id``. That is what turns "a user never sees another
user's memory" from an intention into something a test can check and a reviewer can
verify by reading one directory instead of every call site.

See ADR-0004 for why this service uses SQLAlchemy and Alembic while the MCP server
deliberately does not.
"""

from agent_service.db.base import (
    Base,
    create_db_engine,
    create_session_factory,
    session_scope,
    utc_now,
)

__all__ = [
    "Base",
    "create_db_engine",
    "create_session_factory",
    "session_scope",
    "utc_now",
]
