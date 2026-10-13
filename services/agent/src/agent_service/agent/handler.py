"""The loop, wired to the AgentCore contract.

Two jobs, and they are different enough to keep apart:

**The translation** turns a `TurnResult` into an `InvocationResponse`. It is a pure function,
so it is tested without a database, a provider or a server — and it is where the two
vocabularies meet, which is the only place they should.

**The factory** assembles a loop per invocation: provider, discovered tools, executor, approval
repository, all built from settings and a session. Assembly is per-turn rather than once at
startup because the session and user are per-request, and because a long-lived loop would hold
a database session across requests.

The mapping that matters most is ``stop_reason``. The loop speaks ``end_turn``/``proposed``/
``max_steps``; the contract speaks ``completed``/``awaiting_approval``/``max_steps``. They are
similar enough to pass through by accident and different enough that doing so would be wrong:
``end_turn`` from a provider means "the model finished", while ``completed`` means "this
invocation is done" — which is also true of a proposal. Mapping explicitly means a new loop
stop reason cannot reach the wire unnamed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.orm import Session

from agent_service.api.models import (
    InvocationRequest,
    InvocationResponse,
    PendingApproval,
    StopReason,
    ToolCallRecord,
)
from agent_service.db.models.approval import Approval
from agent_service.db.repositories.approvals import ApprovalRepository
from agent_service.providers.base import Message, ModelProvider
from agent_service.tools.registry import ToolRegistry

from .loop import AgentLoop, LoopLimits, ToolCallRecord as LoopCallRecord, ToolExecutor, TurnResult

#: How the loop's stop reasons map onto the contract's. A table rather than a chain of ifs so
#: an unmapped reason is visible as a missing key rather than as a silent fall-through — and
#: the fallback below is deliberate and named.
STOP_REASON_MAP: dict[str, StopReason] = {
    "end_turn": "completed",
    "proposed": "awaiting_approval",
    "max_steps": "max_steps",
    "deadline": "deadline",
    "cancelled": "cancelled",
}

#: Used when the loop reports a reason this table does not know. ``error`` rather than
#: ``completed``: an unrecognised stop is not an answer, and reporting it as one would tell a
#: caller the turn succeeded when nothing has established that.
UNMAPPED_STOP_REASON: StopReason = "error"

#: The prompt the model is given, before any user text. Written here rather than in a config
#: file because it encodes product decisions — what the agent may claim about its own
#: reliability — and those belong next to the code that depends on them.
SYSTEM_PROMPT = """\
You answer questions about support tickets using the tools you are given.

Three rules, and they are not stylistic:

1. Use a tool before stating a fact about a ticket. If you have not looked something up in
   this conversation, say you would need to look it up rather than answering from memory.
2. Call a write tool when the user asks for a write. Do not ask for confirmation yourself —
   the platform asks a human, and a second confirmation would be confusing. When a write
   needs a `client_request_id`, choose one and reuse it for that same action.
3. If a tool fails, say what failed. Do not retry the same call unchanged and do not claim
   the action succeeded.
"""


def to_invocation_response(
    result: TurnResult,
    *,
    session_id: str,
) -> InvocationResponse:
    """Translate one turn's result into the contract's response.

    Pure, so it is tested directly. The two fields worth explaining are the ones a caller
    branches on:

    ``pending_approval`` carries the *first* pending approval, because the loop proposes at
    most one per turn — a second would need a rule for when it is proposed, and the loop
    refuses it rather than inventing one. So taking the first is not an approximation.

    ``tool_calls`` carries every call with its outcome, including ones that were refused.
    Omitting a refused call would make "I did not try" indistinguishable from "I tried and
    was not allowed", which is exactly the distinction a user needs.
    """
    approval = result.pending[0] if result.pending else None

    return InvocationResponse(
        output=result.text,
        tool_calls=[_record(record) for record in result.tool_calls],
        pending_approval=_pending(approval) if approval is not None else None,
        session_id=session_id,
        stop_reason=STOP_REASON_MAP.get(str(result.stop_reason), UNMAPPED_STOP_REASON),
        steps=result.steps,
    )


def _record(record: LoopCallRecord) -> ToolCallRecord:
    """One loop record as a contract record.

    ``is_error`` is derived from the outcome rather than from the tool's text: a refused call
    and a call whose tool returned an error are both things the user should see as not having
    produced an answer, and keying on the text would miss the first.
    """
    return ToolCallRecord(
        name=record.call.name,
        arguments=dict(record.call.arguments),
        is_error=record.outcome != "executed",
        summary=record.detail,
    )


def _pending(approval: Approval) -> PendingApproval:
    """One approval row as the contract's proposal.

    Carries ``expires_at`` because the decision is time-limited and a client that does not know
    when would show an approval button that fails when pressed.
    """
    return PendingApproval(
        approval_id=approval.id,
        tool_name=approval.action,
        arguments=dict(approval.payload),
        summary=(
            f"The agent wants to call {approval.action}. Approving this runs it with exactly "
            f"the arguments shown."
        ),
        expires_at=approval.expires_at,
    )


class InvocationHandlerFactory:
    """Assembles a loop per invocation and runs one turn through it.

    Holds the long-lived pieces — provider, tools, executor — and builds the short-lived ones
    (a loop bound to this session and user) on each call. A loop cannot be long-lived: it
    carries a session id, and reusing one across requests would attribute every later turn to
    the first caller.
    """

    def __init__(
        self,
        *,
        provider: ModelProvider,
        tools: ToolRegistry,
        executor: ToolExecutor,
        limits: LoopLimits | None = None,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._executor = executor
        self._limits = limits or LoopLimits()
        self._system_prompt = system_prompt

    async def __call__(
        self,
        request: InvocationRequest,
        session: Session,
        history: Sequence[Message] = (),
    ) -> InvocationResponse:
        """Run one invocation.

        ``session`` is supplied by the caller rather than opened here, so the transaction
        boundary stays with the layer that owns it — approve-then-execute must be atomic, and
        only the caller knows where that boundary is.
        """
        session_id = request.session_id or f"sess-{uuid.uuid4().hex}"
        loop = AgentLoop(
            provider=self._provider,
            tools=self._tools,
            executor=self._executor,
            approvals=ApprovalRepository(session),
            session_id=session_id,
            user_id=request.user_id,
            limits=self._limits,
            system_prompt=self._system_prompt,
        )

        result = await loop.run(request.prompt, history=history)
        return to_invocation_response(result, session_id=session_id)
