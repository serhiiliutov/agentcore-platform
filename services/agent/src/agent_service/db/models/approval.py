"""Approvals: the record of a proposed write that a human has not yet allowed.

This table is where the assignment's central guarantee lives. A write tool called by
the model does not execute; it produces a row here, and the row is the only thing
that can later authorise the call. Three properties make that real rather than
decorative:

1. **Everything needed to execute is stored.** ``action`` plus ``payload`` is the
   complete instruction. Execution never goes back to the model for parameters, so a
   provider outage or a new conversation cannot change what was approved. Approving
   "create a ticket" and executing something else is impossible by construction.
2. **The state is a closed set.** A check constraint lists the legal values, so a bug
   that writes ``"aproved"`` fails at the database rather than producing an approval
   that silently never executes.
3. **Pending state survives a restart.** It is a row, not an in-memory object, so the
   process can be redeployed between the proposal and the answer — which is exactly
   the situation the assignment asks about.

``expires_at`` exists because an approval is a decision about *now*. A proposal left
untouched for a day should not be executable tomorrow, when the ticket may be
pointless or the user may have forgotten proposing it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agent_service.db.base import Base, utc_now
from agent_service.db.models.conversation import ChatSession


class ApprovalState(StrEnum):
    """The lifecycle of a proposed write.

    ``pending``   waiting for a human decision.
    ``approved``  allowed, not yet carried out.
    ``declined``  refused by the human; terminal.
    ``expired``   nobody decided in time; terminal.
    ``executed``  carried out successfully; terminal.
    ``failed``    the write was attempted and the backend refused it; terminal.
    """

    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"


#: States from which no further transition is possible.
TERMINAL_STATES: frozenset[ApprovalState] = frozenset(
    {
        ApprovalState.DECLINED,
        ApprovalState.EXPIRED,
        ApprovalState.EXECUTED,
        ApprovalState.FAILED,
    }
)

#: The complete set of legal state values, used to build the check constraint.
_STATE_VALUES = ", ".join(f"'{state.value}'" for state in ApprovalState)


class Approval(Base):
    """A write the model proposed and a human has to allow."""

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(f"state IN ({_STATE_VALUES})", name="state_is_known"),
        # The hot query: "is there a pending approval for this session?"
        Index("ix_approvals_session_state", "session_id", "state"),
        # The sweep: "which pending approvals have expired?"
        Index("ix_approvals_state_expires", "state", "expires_at"),
    )

    #: UUID string. Appears in the API response and in every trace for this decision.
    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    session_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
    )
    #: The user who must decide, and the only user allowed to see or act on this row.
    user_id: Mapped[str] = mapped_column(String(128), index=True)

    #: The tool the model wanted to call, e.g. ``create_ticket``.
    action: Mapped[str] = mapped_column(String(64))
    #: The exact arguments it wanted to call it with. Never re-derived.
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)

    state: Mapped[str] = mapped_column(String(16), default=ApprovalState.PENDING.value)

    #: The idempotency key used when this is executed. Unique, so a double execution
    #: cannot create two tickets even under a retry.
    client_request_id: Mapped[str] = mapped_column(String(64), unique=True)

    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    #: After this moment the proposal is no longer executable.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    #: What the tool returned, once it ran. Stored so a later question about the
    #: ticket can be answered from our own record rather than another read.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    #: Why execution failed, in the words the user will see.
    error: Mapped[str | None] = mapped_column(Text, default=None)

    session: Mapped[ChatSession] = relationship(back_populates="approvals")

    def __repr__(self) -> str:
        return (
            f"Approval(id={self.id!r}, action={self.action!r}, "
            f"state={self.state!r}, user_id={self.user_id!r})"
        )
