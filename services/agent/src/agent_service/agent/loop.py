"""The agent loop, and the one branch it exists to make correctly.

The loop is small on purpose. A provider produces a reply; if the reply wants tools, each call
is either *executed* or *recorded as a proposed write*; results are fed back; repeat until the
model answers or the budget runs out. Almost every line below is that, plus the reasoning for
why the exceptions to it are exceptions.

Four decisions carry the weight:

**A write is never executed here.** When the registry says a tool needs approval, the loop
records a pending row and does *not* call the tool. There is no configuration flag to change
this and no code path around it: the only way a write runs is a separate entrypoint that
demands an approved row. That is what makes the guarantee structural rather than a policy the
loop promises to honour.

**A proposal ends the turn.** After recording a pending write the loop stops and returns, rather
than continuing to reason. It has to: the write's result does not exist yet, so continuing would
ask the model to reason about a ticket that has not been created. The alternative — a
placeholder result such as "pending" — invites the model to summarise a write that may be
refused, and a summary of an action that never happened is worse than no summary.

**A tool failure is fed back, not raised.** The MCP server returns outcomes rather than
exceptions for exactly this reason (Stage 5): a model told *why* a call failed can fix its
arguments, and a model handed a stack trace cannot. The loop therefore catches transport
failures too and turns them into a result the model can read.

**The budget is a turn count and it is enforced here.** A provider is given the remaining
allowance as a hint, but the ceiling is this loop's own, so a provider that ignores it — or a
model that keeps calling tools — cannot make a conversation unbounded.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent_service.agent.budget import (
    CancelToken,
    Deadline,
    DeadlineExceededError,
    RunCancelledError,
)
from agent_service.db.models.approval import Approval
from agent_service.db.repositories.approvals import ApprovalRepository
from agent_service.providers.base import (
    AssistantTurn,
    Message,
    ModelProvider,
    StopReason,
    ToolCallRequest,
)
from agent_service.tools.registry import ToolRegistry

#: Why a turn ended. ``proposed`` is deliberately distinct from ``end_turn``: the model did not
#: finish, it stopped because a human is now the blocker, and a caller that treated the two the
#: same would report a completed answer where none exists.
TurnStop = StopReason | str


class ToolExecutor(Protocol):
    """How the loop runs a read-only tool.

    A Protocol, as in Stages 4 and 6, so a test drives the loop with a scripted executor and no
    server — while the real MCP client is still checked against the same shape. Note the
    signature returns a *string*: this is the result as a model will read it, which is what the
    MCP server already produces and what the loop feeds back.
    """

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Run one tool and return its result as text."""
        ...


@dataclass(frozen=True)
class LoopLimits:
    """The ceilings on one turn."""

    #: Maximum provider round trips. Each may carry several tool calls.
    max_steps: int = 8
    #: How long a proposed write stays executable.
    approval_ttl_seconds: int = 1800


@dataclass(frozen=True)
class ToolCallRecord:
    """What the loop did with one requested call, for the trace and the response.

    ``outcome`` is one of ``executed``, ``proposed`` or ``refused``. It is recorded per call
    rather than aggregated because a turn may mix them — one search that ran and one write that
    now needs approval — and a caller needs to report both accurately.
    """

    call: ToolCallRequest
    outcome: str
    detail: str = ""


@dataclass(frozen=True)
class TurnResult:
    """What one turn produced."""

    text: str
    stop_reason: TurnStop
    #: Populated when the loop stopped to await a decision.
    pending: tuple[Approval, ...] = ()
    tool_calls: tuple[ToolCallRecord, ...] = ()
    #: How many provider round trips were used. For the trace and for the evals.
    steps: int = 0
    usage: Any = field(default=None)

    @property
    def awaits_approval(self) -> bool:
        """Whether a human is now the blocker."""
        return bool(self.pending)


#: Appended to the conversation when a tool could not be run at all. Says what to do next,
#: because a model told only that something failed will usually retry it identically.
TRANSPORT_FAILURE_TEMPLATE = (
    "ERROR: the tool could not be reached ({reason}). "
    "Do not retry the same call unchanged; report the problem instead."
)


class AgentLoop:
    """Orchestrates one turn: provider, registry, executor and the approval gate."""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        tools: ToolRegistry,
        executor: ToolExecutor,
        approvals: ApprovalRepository,
        session_id: str,
        user_id: str,
        limits: LoopLimits | None = None,
        system_prompt: str = "",
        deadline: Deadline | None = None,
        cancel: CancelToken | None = None,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._executor = executor
        self._approvals = approvals
        self._session_id = session_id
        self._user_id = user_id
        self._limits = limits or LoopLimits()
        self._system_prompt = system_prompt
        #: Wall-clock allowance for the whole turn, shared with the executor. Optional so a test
        #: can drive the loop with no clock at all.
        self._deadline = deadline
        #: Someone else's flag. The loop reads it, never sets it — the reason a run was cancelled
        #: is not something the run gets to author.
        self._cancel = cancel

    async def run(self, prompt: str, *, history: Sequence[Message] = ()) -> TurnResult:
        """Run one turn to its end — an answer, a proposal, or the budget.

        ``history`` is the conversation so far, supplied by the caller rather than held here, so
        the loop stays stateless and two turns cannot share mutable state by accident. Stage 9
        supplies it from the database.
        """
        messages = self._opening(prompt, history)
        records: list[ToolCallRecord] = []
        proposals: list[Approval] = []
        last_usage = None
        steps = 0

        try:
            for step in range(1, self._limits.max_steps + 1):
                # Checked *between* steps, never inside one. A step already running is never torn
                # down, which is what keeps a write that is executing able to finish and be
                # recorded — see the module docstring on why that matters.
                self._checkpoint()
                # Counted only once the step is actually going to run. Incrementing before the
                # checkpoint would report a step that was announced and then abandoned — and a
                # trace claiming a round trip that never happened is worse than no count, because
                # it is the number somebody would use to explain a slow turn.
                steps = step

                turn = await self._provider.chat(
                    messages,
                    self._tools.as_provider_tools(),
                    budget=self._limits.max_steps - step,
                )
                last_usage = turn.usage

                if not turn.wants_tools:
                    return TurnResult(
                        text=turn.text,
                        stop_reason=turn.stop_reason,
                        tool_calls=tuple(records),
                        steps=step,
                        usage=last_usage,
                    )

                # The assistant turn is appended before execution, because every provider
                # requires the call and its result to correspond when the conversation
                # continues.
                messages.append(Message.assistant(turn.text, turn.tool_calls))

                # A proposal stops the turn. See the module docstring: the result does not exist
                # yet, so there is nothing truthful to feed back.
                proposal = self._propose_if_write(turn, records)
                if proposal is not None:
                    proposals.append(proposal)
                    return TurnResult(
                        text=(
                            f"{turn.text}\n\n"
                            f"This needs your approval before it runs "
                            f"(approval {proposal.id}, action {proposal.action})."
                        ).strip(),
                        stop_reason="proposed",
                        pending=tuple(proposals),
                        tool_calls=tuple(records),
                        steps=step,
                        usage=last_usage,
                    )

                await self._execute_reads(turn, messages, records)
        except RunCancelledError as exc:
            # A state, not a failure. The partial transcript is kept and the reason is carried: a
            # caller handed an exception cannot tell "the user changed their mind" from "the
            # process is dying", and those want opposite responses.
            return TurnResult(
                text=f"I stopped because the run was cancelled: {exc}",
                stop_reason="cancelled",
                tool_calls=tuple(records),
                steps=steps,
                usage=last_usage,
            )
        except DeadlineExceededError as exc:
            # Said as a thing that gave up, not as a thing that broke. A turn that ran out of
            # time is not an error state a user should retry blindly, and it must not look like
            # one in the trace.
            return TurnResult(
                text=f"I gave up on this turn: {exc}",
                stop_reason="timed_out",
                tool_calls=tuple(records),
                steps=steps,
                usage=last_usage,
            )

        # The ceiling, not a failure: a model that kept calling tools was cut off, which is a
        # fact about this turn and worth reporting as such.
        return TurnResult(
            text="I stopped before finishing: the step limit for one turn was reached.",
            stop_reason="max_steps",
            tool_calls=tuple(records),
            steps=self._limits.max_steps,
            usage=last_usage,
        )

    def _opening(self, prompt: str, history: Sequence[Message]) -> list[Message]:
        """The conversation to send: system, prior turns, then this prompt."""
        messages: list[Message] = []
        if self._system_prompt:
            messages.append(Message.system(self._system_prompt))
        messages.extend(history)
        messages.append(Message.user(prompt))
        return messages

    def _propose_if_write(
        self, turn: AssistantTurn, records: list[ToolCallRecord]
    ) -> Approval | None:
        """Record the first write in this reply as pending, if there is one.

        Only the first, and the turn ends. A reply asking for two writes would otherwise need a
        rule for what to do with the second — propose it in the same turn, or after the first is
        decided — and either answer invents behaviour the model cannot have intended, since it
        could not know the first would need asking.
        """
        for call in turn.tool_calls:
            if not self._tools.requires_approval(call.name):
                continue

            approval = self._approvals.propose(
                session_id=self._session_id,
                user_id=self._user_id,
                action=call.name,
                payload=dict(call.arguments),
                ttl_seconds=self._limits.approval_ttl_seconds,
                # The key the model chose, when it chose one. It must be the key used at
                # proposal time so a re-execution after approval is provably the same action.
                client_request_id=_declared_key(call),
            )
            records.append(
                ToolCallRecord(
                    call=call,
                    outcome="proposed",
                    detail=f"awaiting approval {approval.id}",
                )
            )
            return approval

        return None

    async def _execute_reads(
        self, turn: AssistantTurn, messages: list[Message], records: list[ToolCallRecord]
    ) -> None:
        """Run every non-write call and append each result.

        A call the registry does not hold is refused rather than attempted. The provider cannot
        produce one from a schema it was given, but a replay or a hand-written recording could,
        and calling an unpublished name would reach the server as a protocol error rather than
        as the clear statement this produces.
        """
        for call in turn.tool_calls:
            if self._tools.requires_approval(call.name):
                # Already handled: the first write ended the turn, so this can only be a second
                # one. Recorded so the response does not silently omit it.
                records.append(
                    ToolCallRecord(
                        call=call,
                        outcome="refused",
                        detail="a second write in one turn; only the first was proposed",
                    )
                )
                messages.append(
                    Message.tool_result(
                        call,
                        "ERROR: only one write can be proposed per turn. "
                        "This one was not proposed; ask again once the first is decided.",
                    )
                )
                continue

            if call.name not in self._tools:
                records.append(ToolCallRecord(call=call, outcome="refused", detail="unknown tool"))
                messages.append(
                    Message.tool_result(call, f"ERROR: no tool named {call.name!r} is available.")
                )
                continue

            text = await self._call(call)
            records.append(ToolCallRecord(call=call, outcome="executed", detail=""))
            messages.append(Message.tool_result(call, text))

    def _checkpoint(self) -> None:
        """Stop between steps if the run was cancelled or the turn is out of time.

        One method rather than two checks because they are checked at exactly the same moments
        for the same reason, and having them in one place means a future step cannot be added
        with only one of them. A missing cancel check is a run that will not stop; a missing
        deadline check is a turn with no ceiling. Neither fails loudly at review time.
        """
        if self._cancel is not None:
            self._cancel.raise_if_cancelled()
        if self._deadline is not None and self._deadline.expired:
            raise DeadlineExceededError(self._deadline.budget_s, self._deadline.spent_s)

    async def _call(self, call: ToolCallRequest) -> str:
        """Run one tool, turning a transport failure into something the model can read."""
        try:
            return await self._executor.call(call.name, dict(call.arguments))
        except Exception as exc:
            # Deliberately broad. The alternative to catching here is a traceback, which ends
            # the turn with nothing the model can act on — and the failures worth catching are
            # exactly the ones the transport raises, which are not a closed set the loop can
            # enumerate. The message names the reason and tells the model not to retry
            # unchanged, because a model told only that something failed usually does.
            return TRANSPORT_FAILURE_TEMPLATE.format(reason=type(exc).__name__)


def _declared_key(call: ToolCallRequest) -> str | None:
    """The ``client_request_id`` the model supplied, if it supplied a string.

    Passed through when present so the key is the model's own — which is what makes a retried
    proposal reuse it — and left to the repository to mint when absent. Not validated further
    here: the MCP server owns that contract, and duplicating its rules would give two answers.
    """
    declared = call.arguments.get("client_request_id")
    return declared if isinstance(declared, str) and declared else None
