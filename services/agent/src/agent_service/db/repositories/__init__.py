"""Repositories: the only place that reads or writes the database directly.

Deferred from Stage 2 on purpose. A repository written before its caller exists has no
requirements to satisfy and gets shaped by guesswork — the approval repository arrived with
Stage 8, which needed it, and the memory repositories arrive with Stage 9 for the same reason.

Every method that touches user-owned data takes a ``user_id``. That is not decoration: it is
the invariant Stage 2 built into the schema, enforced again at the seam where a caller could
otherwise forget it.
"""

from __future__ import annotations

from .approvals import ApprovalRepository, ApprovalTransitionError

__all__ = ["ApprovalRepository", "ApprovalTransitionError"]
