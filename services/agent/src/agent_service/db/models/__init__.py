"""The agent's schema, one module per aggregate.

Importing every model here is not tidiness — it is required. Alembic's autogenerate
compares the live database against ``Base.metadata``, and a model whose module was
never imported is absent from that metadata. The failure mode is unpleasant: a table
that exists in code, is missing from every migration, and only shows up as a
``no such table`` error in a fresh deployment.

So this module is the single place the schema is enumerated, and every model is
re-exported with an explicit ``__all__`` so that "what tables exist" has one answer.

The aggregates mirror the assignment's concerns:

``conversation``   sessions and messages — what was said
``approval``       proposed writes and their lifecycle — what must be allowed
``memory``         durable per-user facts — what is remembered, and for how long
``operations``     idempotency ledger and provider selection — running safely
``observability``  the trace index — explaining a run afterwards
"""

from agent_service.db.models.approval import (
    TERMINAL_STATES,
    Approval,
    ApprovalState,
)
from agent_service.db.models.conversation import (
    MESSAGE_ROLES,
    ChatSession,
    Message,
)
from agent_service.db.models.memory import UserFact
from agent_service.db.models.observability import Trace
from agent_service.db.models.operations import IdempotencyKey, ProviderSetting

__all__ = [
    "MESSAGE_ROLES",
    "TERMINAL_STATES",
    "Approval",
    "ApprovalState",
    "ChatSession",
    "IdempotencyKey",
    "Message",
    "ProviderSetting",
    "Trace",
    "UserFact",
]
