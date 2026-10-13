"""The AgentCore Runtime contract's request and response shapes.

Written as pydantic models rather than documented in prose because the contract is
enforced, not described: the platform sends these fields, and a request that does not match
is rejected with a 422 before any orchestration begins. `CONTRACT.md` at the repository root
explains the same shapes in words; this module is what makes them true.

Three choices here are decisions rather than transcriptions.

**``user_id`` is required.** Memory is user-scoped — Stage 2 made every read of user-owned
data take a ``user_id`` — so an invocation without one could not safely read or write a
single fact. Making it optional and defaulting to something would have quietly broken the
isolation the schema enforces.

**``session_id`` is optional and means "continue".** Omitting it starts a new conversation;
supplying one resumes that conversation's history. The runtime's own session header maps
onto this in `app.py`, so a client that sets a header and a client that sets this field get
the same behaviour.

**``pending_approval`` is part of the response, not an error.** A turn that ended by
proposing a write is a *successful* turn — the agent did its job and is waiting for a human.
Folding it into an error path would make the common case of the approval workflow look like
a failure to every monitoring tool watching status codes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

#: The two states the AgentCore Runtime health contract allows. A Literal so returning a
#: third value is impossible rather than merely wrong.
HealthStatus = Literal["Healthy", "HealthyBusy"]

#: Why the loop stopped. Returned so a caller can distinguish "answered", "needs a human"
#: and "hit a limit" without inferring it from whichever fields happen to be present.
StopReason = Literal[
    "completed",
    "awaiting_approval",
    "max_steps",
    "deadline",
    "cancelled",
    "error",
]

#: A non-empty prompt. An empty string is a client bug and is rejected as one.
PROMPT_MIN = 1
#: Bounded so a single invocation cannot be used to push megabytes through the model.
PROMPT_MAX = 8000


class InvocationRequest(BaseModel):
    """One turn of conversation, as the platform sends it."""

    model_config = ConfigDict(extra="ignore")

    prompt: Annotated[str, StringConstraints(min_length=PROMPT_MIN, max_length=PROMPT_MAX)]
    #: Required. See the module docstring: without it, nothing user-scoped may be read.
    user_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    #: Omit to start a new conversation; supply to continue an existing one.
    session_id: str | None = None


class ToolCallRecord(BaseModel):
    """One tool call the agent made, recorded for the caller's benefit.

    Kept in the response rather than only in a log because the caller is usually the person
    who asked the question. "I searched for X and found nothing" is a different answer from
    "I did not search", and only the record distinguishes them.
    """

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    is_error: bool = False
    #: The tool's own sentence, already written for a reader.
    summary: str = ""


class PendingApproval(BaseModel):
    """A write the agent wants to perform, waiting for a human decision.

    The whole point of the approval gate is that this describes the action *and* the exact
    arguments, so approving it approves something specific. A proposal that carried only a
    description would let the executed call differ from the approved one.
    """

    approval_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: One sentence for the person deciding, written by the agent.
    summary: str = ""
    #: When this proposal stops being approvable. Past it, the decision is refused rather
    #: than executed late.
    expires_at: datetime


class InvocationResponse(BaseModel):
    """One turn's result."""

    #: The agent's answer to the user. Empty is legitimate when the turn ended by
    #: proposing a write, since the agent said what it needs through `pending_approval`.
    output: str = ""
    #: Every tool call made this turn, in order.
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    #: Present only when the turn is waiting on a human.
    pending_approval: PendingApproval | None = None
    #: The conversation this turn belongs to, so the client can continue it.
    session_id: str
    #: Why the loop stopped. Never inferred from which fields are populated.
    stop_reason: StopReason = "completed"
    #: How many model turns were used, against the configured ceiling.
    steps: int = 0


class HealthResponse(BaseModel):
    """``GET /ping`` — the AgentCore health contract.

    ``HealthyBusy`` is reported while an invocation is in flight, so a supervisor can tell a
    working process under load from an idle one. It is not an error state: the runtime is
    meant to keep routing to a busy instance, and reporting ``Healthy`` under load would
    hide a saturated service.
    """

    status: HealthStatus
