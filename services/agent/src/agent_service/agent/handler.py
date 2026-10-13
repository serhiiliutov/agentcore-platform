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
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session

from agent_service.agent.budget import Deadline
from agent_service.agent.inflight import InflightRegistry
from agent_service.api.models import (
    InvocationRequest,
    InvocationResponse,
    PendingApproval,
    StopReason,
    ToolCallRecord,
)
from agent_service.db.models.approval import Approval
from agent_service.db.repositories.approvals import ApprovalRepository
from agent_service.db.repositories.sessions import SessionRepository
from agent_service.db.repositories.traces import TraceRepository
from agent_service.observability import MODEL_TURN, Tracer
from agent_service.providers.base import Message, ModelProvider
from agent_service.tools.registry import ToolRegistry

from .loop import AgentLoop, LoopLimits, ToolCallRecord as LoopCallRecord, ToolExecutor, TurnResult

#: Where traces go when the factory is not told otherwise. Relative rather than absolute so a
#: container can mount it, and overridable so a test never writes to the real directory.
DEFAULT_TRACE_DIR = Path("traces")

#: How long one turn may take. Generous, because it exists to bound a stuck turn rather than to
#: hurry a working one: a model making three tool calls legitimately takes tens of seconds, and a
#: budget tight enough to interrupt that would turn normal behaviour into a timeout.
DEFAULT_TURN_BUDGET_S = 90.0

#: How the loop's stop reasons map onto the contract's. A table rather than a chain of ifs so
#: an unmapped reason is visible as a missing key rather than as a silent fall-through — and
#: the fallback below is deliberate and named.
#:
#: The keys are the *loop's* vocabulary and the values are the *contract's*, which is why
#: ``timed_out`` becomes ``deadline``: the loop names what happened to it, and the wire names
#: what a caller should do about it. Every key here is a reason the loop can actually produce —
#: a table entry for a reason nothing emits is a translation nothing tests.
STOP_REASON_MAP: dict[str, StopReason] = {
    "end_turn": "completed",
    "proposed": "awaiting_approval",
    "max_steps": "max_steps",
    "timed_out": "deadline",
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


def _trace_status(result: TurnResult) -> str:
    """How the run should be described in the trace row.

    Only a genuine ``error`` stop is a failed trace. A turn that ended by *proposing* a write is a
    success — the agent did its job — and marking it as an error would make the approval workflow
    show up in every failure listing, which is precisely the confusion the contract's own
    ``stop_reason`` was designed to avoid at the HTTP layer.
    """
    return "error" if result.stop_reason == "error" else "ok"


def _turn_messages(prompt: str, result: TurnResult) -> tuple[Message, ...]:
    """The two sides of one turn, ready to store.

    Only the prompt and the final answer are recorded, not the intermediate tool calls. Storing
    those would make memory a transcript, and this service's memory is deliberately a
    conversation: the model does not need to be shown its own past searches to answer the next
    question, and carrying them would grow every prompt with text that has already been acted
    on. The tool calls are reported to the *caller* in the response, which is where they belong.

    An assistant reply that is empty — the case when the turn ended by proposing a write — is
    stored as it is rather than skipped, because the next turn should see that the agent was
    asked something and answered with a proposal rather than believing the question was never
    asked.
    """
    return (
        Message(role="user", content=prompt),
        Message(role="assistant", content=result.text),
    )


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
        pending_approval=as_pending_approval(approval) if approval is not None else None,
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


def as_pending_approval(approval: Approval) -> PendingApproval:
    """One approval row as the contract's proposal.

    Carries ``expires_at`` because the decision is time-limited and a client that does not know
    when would show an approval button that fails when pressed.

    Public because two callers need it: the invocation response, and the web page's listing of
    what is still waiting. They must produce the same shape and the same sentence — a proposal
    that reads differently depending on which endpoint delivered it is a proposal a user has to
    re-read to decide, and the two would drift the first time either changed.
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
        trace_dir: Path | None = None,
        trace_warnings: Callable[[BaseException], None] | None = None,
        runs: InflightRegistry | None = None,
        turn_budget_s: float = DEFAULT_TURN_BUDGET_S,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._executor = executor
        self._limits = limits or LoopLimits()
        self._system_prompt = system_prompt
        self._trace_dir = trace_dir or DEFAULT_TRACE_DIR
        #: Told when a trace could not be written. Passed in rather than only logged, so a caller
        #: can count or alert on it; the tracer logs regardless.
        self._trace_warnings = trace_warnings
        #: Where this turn registers itself so a cancel can find it. Optional, because a test that
        #: drives one turn does not need a registry — but production always supplies one, or the
        #: cancel endpoint could only ever answer "nothing was running".
        self._runs = runs
        #: How long one turn may take in wall-clock time. A budget per turn rather than a timeout
        #: per call, so an operator can answer "why did that take forty seconds" — see
        #: `agent/budget.py`.
        self._turn_budget_s = turn_budget_s

    async def __call__(
        self,
        request: InvocationRequest,
        session: Session,
    ) -> InvocationResponse:
        """Run one invocation.

        ``session`` is supplied by the caller rather than opened here, so the transaction
        boundary stays with the layer that owns it — approve-then-execute must be atomic, and
        only the caller knows where that boundary is.

        This method, not its caller, now owns conversation memory. It used to take a ``history``
        argument and Stage 8 passed nothing; the fix is not for callers to start building
        history but for this layer to do it, because every caller would otherwise have to
        remember to, and one that forgot would silently produce a stateless agent that still
        looked like it was working.

        Order matters and is not incidental: ``ensure`` runs before anything session-scoped,
        because the session row is what a proposed write's foreign key points at. Without it the
        first write in a fresh conversation fails on a constraint — a certain failure, not a
        possible one, and exactly the gap Stage 8 recorded.

        The turn is stored after the loop returns, and the assistant's reply with it, so the next
        turn of the same conversation sees both sides. A turn that ended by proposing a write is
        stored too: the proposal is part of what happened.
        """
        session_id = request.session_id or f"sess-{uuid.uuid4().hex}"
        sessions = SessionRepository(session)
        sessions.ensure(session_id, user_id=request.user_id)
        history = sessions.history(session_id, user_id=request.user_id)

        # Registered before the loop runs, so a cancel arriving at any point during the turn finds
        # a token to set. Registering after would leave a window in which the endpoint answers
        # "nothing was running" for a turn that is very much running.
        #
        # Bound to a local first: the `finally` below cannot narrow an attribute, and a registry
        # that could be swapped mid-turn is not a thing this is prepared to reason about.
        runs = self._runs
        run = (
            runs.start(session_id=session_id, user_id=request.user_id) if runs is not None else None
        )

        loop = AgentLoop(
            provider=self._provider,
            tools=self._tools,
            executor=self._executor,
            approvals=ApprovalRepository(session),
            session_id=session_id,
            user_id=request.user_id,
            limits=self._limits,
            system_prompt=self._system_prompt,
            deadline=Deadline(budget_s=self._turn_budget_s),
            cancel=run.token if run is not None else None,
        )

        tracer = Tracer(
            trace_dir=self._trace_dir,
            session_id=session_id,
            user_id=request.user_id,
            provider=type(self._provider).__name__,
            on_write_failure=self._trace_warnings,
        )

        try:
            with tracer.span(MODEL_TURN, "invocation", prompt_chars=len(request.prompt)):
                result = await loop.run(request.prompt, history=history)
        finally:
            # In a `finally` rather than after the call: a turn that raised must not stay
            # registered, or the registry would fill with runs that are over and a cancel for one
            # of their sessions would set a token nobody is reading.
            if runs is not None and run is not None:
                runs.finish(run.run_id)

        sessions.append_all(
            session_id,
            user_id=request.user_id,
            messages=_turn_messages(request.prompt, result),
        )

        # Recorded after the turn is stored, so a trace row never describes a turn the
        # conversation does not contain. If storing the turn raised, there would be no trace
        # either — and a trace of a turn that was not saved would be the more misleading of the
        # two artefacts.
        TraceRepository(session).record(
            tracer.finish(
                status=_trace_status(result),
                error=result.text if result.stop_reason == "error" else None,
                model_turns=result.steps,
                tool_calls=len(result.tool_calls),
            ),
            user_id=request.user_id,
        )

        return to_invocation_response(result, session_id=session_id)
