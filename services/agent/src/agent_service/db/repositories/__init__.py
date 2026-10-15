"""Repositories: the only place that reads or writes the database directly.

A repository is written when its caller exists, not before. The approval repository arrived
with Stage 8, which needed it; the memory repositories arrive with Stage 9 for the same
reason — a repository written first has no requirements to satisfy and gets shaped by
guesswork.

Every method that touches user-owned data takes a ``user_id``. That is not decoration: it is
the invariant Stage 2 built into the schema, enforced again at the seam where a caller could
otherwise forget it.

The four repositories cover the four kinds of state this service keeps, and the split is by
lifetime rather than by table:

* `SessionRepository` — this conversation. Bounded, and meaningful only while it is current.
* `FactRepository` — this person. Survives conversations, and expires on its own schedule.
* `ApprovalRepository` — this decision. Terminal once made, and the only key to a write.
* `TraceRepository` — this run. Written once at the end, and read afterwards by an operator.

`TraceRepository` arrived with Stage 10, which is the same rule applied again: it exists because
the tracer needed somewhere to put a summary, not because a plan listed it in advance.

`ProviderSettingsRepository` is the one that arrived *late* on purpose. The table was declared
early and left unwired for several stages, because a table is cheap and a caller is not: writing
this repository before something needed to change the provider would have shaped it by guesswork.
It was written when the page gained a provider picker, which is the caller that gave it
requirements — one stored choice, readable at startup, replaceable rather than accumulating.
"""

from __future__ import annotations

from .approvals import ApprovalRepository, ApprovalTransitionError
from .facts import FactRepository
from .provider_settings import GLOBAL_SCOPE, ProviderSettingsRepository
from .sessions import DEFAULT_HISTORY_LIMIT, SessionRepository
from .traces import DEFAULT_LIST_LIMIT, STATUSES, TraceRepository

__all__ = [
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_LIST_LIMIT",
    "GLOBAL_SCOPE",
    "STATUSES",
    "ApprovalRepository",
    "ApprovalTransitionError",
    "FactRepository",
    "ProviderSettingsRepository",
    "SessionRepository",
    "TraceRepository",
]
