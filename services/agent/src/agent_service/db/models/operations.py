"""Operational tables: the idempotency ledger and the selected model provider.

These are not about the conversation; they are about running the service safely.

**``IdempotencyKey``** is the agent's own ledger, separate from the MCP server's on
purpose. AgentCore retries ``/invocations`` on a timeout, and a retried invocation
that is not recognised would run the model again — paying for a second turn, and
possibly proposing a second write the user already approved once. This renders a
retried request into the same response as the first.

``request_fingerprint`` is what makes it honest. Keying on the id alone would collapse
two genuinely different requests that happen to share an id into one answer, so a hash
of the prompt is stored alongside and a mismatch is an error rather than a silent
replay of the wrong answer.

**``ProviderSetting`` is reserved, and currently unwired.** No code path reads or writes a
row, and the provider in force is ``Settings.agent_model_provider``, read once at startup
(`providers/base.py`). It is kept rather than dropped because the design it belongs to is
sound and half-built: a row instead of process state would make a provider change survive a
restart and be visible to a reviewer, and ``scope`` would allow a per-session override without
inventing a second mechanism. Whoever wires it should change the resolution point in
`providers/base.py` and delete this paragraph. Until then the honest statement — the one
`docs/runbooks/provider-setup.md` now makes — is that changing provider means changing the
environment and restarting.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from agent_service.db.base import Base, utc_now


class IdempotencyKey(Base):
    """One recognised request, and the response it already produced."""

    __tablename__ = "idempotency_keys"

    #: The client-supplied request id.
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    #: Which operation this key belongs to, so ids cannot collide across endpoints.
    scope: Mapped[str] = mapped_column(String(64), default="invocations")

    session_id: Mapped[str | None] = mapped_column(String(128), default=None, index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)

    #: A hash of the request. A reused key with different content is a client bug and
    #: must be reported, not silently answered with the previous response.
    request_fingerprint: Mapped[str] = mapped_column(String(64))

    #: The response that was returned, replayed verbatim on a repeat.
    response: Mapped[dict[str, Any]] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    #: Kept long enough that a delayed retry still matches, then pruned. An unbounded
    #: ledger is a slow leak. Indexed because the sweep finds old entries by age.
    #:
    #: ``index=True`` rather than an explicit ``Index`` in ``__table_args__``: the naming
    #: convention already derives ``ix_idempotency_keys_expires_at`` from this column, so
    #: declaring it both ways produced two indexes under one name and ``create_all`` failed
    #: on the second. The migration never noticed, because Alembic emitted a single index and
    #: nothing had called ``create_all`` on this metadata until Stage 8's tests did.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    def __repr__(self) -> str:
        return f"IdempotencyKey(key={self.key!r}, scope={self.scope!r})"


class ProviderSetting(Base):
    """Which model provider and model are selected, for one scope.

    Reserved and unwired: see the module docstring. The columns are the design; nothing
    reads or writes them, and ``scope``'s per-session idea is not implemented.
    """

    __tablename__ = "provider_settings"

    #: ``global`` for the service default, or ``session:<id>`` for an override.
    scope: Mapped[str] = mapped_column(String(128), primary_key=True)

    #: One of the provider names in ``config.ProviderName``.
    provider: Mapped[str] = mapped_column(String(32))
    #: Empty means the provider's own default model.
    model_id: Mapped[str] = mapped_column(String(128), default="")

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    #: Free-text note on who changed this and why. Operators change provider settings
    #: during an incident; the next person should not have to guess.
    note: Mapped[str | None] = mapped_column(Text, default=None)

    def __repr__(self) -> str:
        return f"ProviderSetting(scope={self.scope!r}, provider={self.provider!r})"
