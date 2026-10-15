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

import logging
import time
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from agent_service.agent.budget import (
    CancelToken,
    Deadline,
    DeadlineExceededError,
    RunCancelledError,
)
from agent_service.db.models.approval import Approval
from agent_service.db.repositories.approvals import ApprovalRepository
from agent_service.observability.spans import PROPOSAL, TOOL_CALL
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


@dataclass(frozen=True)
class ToolOutcome:
    """What one tool call produced: the text a model reads, and whether it worked.

    The two travel together because the loop needs both and can derive neither from the other.
    The text goes into the conversation; `is_error` is what lets the loop describe the call
    accurately in the response. Returning only the text — which is what this used to do — left
    the loop unable to tell a search that found rows from one the backend refused, so every call
    was recorded as having executed and a refusal was reported to the caller as an answer.

    Deliberately not inferred from the text. The MCP layer already knows whether the tool
    failed, and a prefix convention here would make the loop depend on the exact wording of a
    message it does not own — so a reworded error would quietly start reading as a success.
    """

    text: str
    is_error: bool = False
    #: A one-line description of the call, for the *response* rather than for the model.
    #:
    #: Separate from ``text`` because the two readers want opposite things from one call. The model
    #: needs the returned **data** — a page of tickets, a ticket's comments, each ticket's category —
    #: or it cannot answer without inventing; a person watching the transcript needs one line they
    #: can scan. Sending ``text`` to the response would put a page of JSON in every tool row, and
    #: sending the response's sentence to the model is precisely the defect that let a live run
    #: fabricate twenty-five tickets. Empty means "``text`` is already short enough", which is the
    #: case for a transport failure, where the two are the same sentence.
    summary: str = ""
    #: The backend answered "already done" rather than doing it again. Only writes can set this,
    #: and it is carried as data rather than recovered from ``text`` for the same reason
    #: ``is_error`` is: a caller that reads a sentence for a marker is coupled to wording it does
    #: not own, and the ticketing server's sentence for this case — "already existed for this
    #: request" — contains no marker that a search would find.
    replayed: bool = False


class ToolExecutor(Protocol):
    """How the loop runs a read-only tool.

    A Protocol, as in Stages 4 and 6, so a test drives the loop with a scripted executor and no
    server — while the real MCP client is still checked against the same shape. Note the
    signature returns a `ToolOutcome`: the result as a model will read it, *and* whether the call
    succeeded — which the loop needs in order to report the call accurately to the caller.
    """

    async def call(
        self, name: str, arguments: dict[str, Any], *, timeout_s: float | None = None
    ) -> ToolOutcome:
        """Run one tool and return its result.

        ``timeout_s`` is the slice the turn has left, computed by the loop from its own deadline.
        Optional so a test executor need not care about clocks; when omitted, an implementation
        uses whatever ceiling it was built with.

        The parameter exists because a budget that is only checked *between* steps would not bound
        a single slow call — and a turn whose first tool call hangs forever would never reach the
        next checkpoint to notice. The check and the timeout are the same budget applied at the
        two places a step can consume time.
        """
        ...


class SpanRecorder(Protocol):
    """What the loop needs in order to record what it did.

    A Protocol rather than the concrete `Tracer`, for the reason the other seams in this project
    use one: a test drives the loop with a list-appending recorder and no trace directory, while
    the real tracer is still checked against the same shape. The two methods below are the whole
    vocabulary — a unit of work that takes time, and an event that does not.

    Optional everywhere it is used. A loop with no recorder does exactly the same work and simply
    leaves no span behind, which is what keeps observability from becoming a dependency of
    correctness.
    """

    def span(self, kind: str, name: str, **attributes: Any) -> AbstractContextManager[Any]:
        """Record a unit of work, closed when the block ends."""
        ...

    def add(self, kind: str, name: str, **attributes: Any) -> Any:
        """Record an event that happened at an instant."""
        ...


@dataclass(frozen=True)
class LoopLimits:
    """The ceilings on one turn."""

    #: Maximum provider round trips. Each may carry several tool calls.
    max_steps: int = 8
    #: How long a proposed write stays executable.
    approval_ttl_seconds: int = 1800


#: What became of one tool call. Declared here, where the values are decided, and deliberately
#: spelled identically to the published contract: the two are the same four words, so saying it
#: once in a type means a value this loop invents cannot reach the wire unnamed — the checker
#: rejects it at the assignment rather than the response quietly carrying a fifth outcome.
ToolOutcomeValue = Literal["executed", "failed", "proposed", "refused"]


@dataclass(frozen=True)
class ToolCallRecord:
    """What the loop did with one requested call, for the trace and the response.

    ``outcome`` is one of ``executed``, ``failed``, ``proposed`` or ``refused``. It is recorded
    per call rather than aggregated because a turn may mix them — one search that ran and one
    write that now needs approval — and a caller needs to report both accurately.

    ``failed`` exists because ``executed`` alone cannot describe a call the tool itself refused:
    the call ran, the backend said no, and a caller told only ``executed`` would report a refusal
    as an answer. `ToolOutcome.is_error` is where that fact arrives.
    """

    call: ToolCallRequest
    outcome: ToolOutcomeValue
    detail: str = ""
    #: How long the call took, in milliseconds. Measured by the loop so the *response* carries it
    #: as well as the trace: the contract's example response has a ``duration_ms`` per call, and a
    #: caller reading only the response should not have to open a trace file to find the slow one.
    duration_ms: int | None = None


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

#: What the user is told when the *model* could not answer. Deliberately free of the provider's own
#: message, because that message can quote the credential: OpenAI's response for a rejected key
#: includes the first characters of the key it rejected ("Incorrect API key provided: sk-proj-Ab…").
#: Putting that in an HTTP response, or in the conversation stored in the database, would leak key
#: material through a path the whole design exists to keep it out of. The type name and status code
#: are enough for a person to tell a bad key from a network that is down, and the provider's own
#: wording is in the agent's log for whoever is debugging.
PROVIDER_FAILURE_TEMPLATE = (
    "The model could not answer this turn: {reason}. {hint} "
    "The agent's log has the provider's own message."
)

#: What to check, per status code. The status is the one thing we can act on without echoing the
#: provider's sentence, and the wrong advice is worse than none: the first version of this message
#: told every failure to "check the credential and the model id", which is unhelpful for a 429 —
#: a rate limit means the credential was accepted and the *account* is the constraint.
PROVIDER_HINTS: dict[int, str] = {
    401: "The provider rejected the credential. Check the key in the environment.",
    403: "The provider refused this key for that model or region.",
    404: "The provider does not serve that model id. Pick another in the model control.",
    429: (
        "The provider is rate-limiting this key, which usually means the account has no remaining "
        "quota rather than a momentary burst — the request was authenticated, and the client has "
        "already retried. Check billing and usage for the account, or choose another provider."
    ),
}

#: Used for a status with no hint above, and for a failure with no status at all — a timeout or a
#: dropped connection, where the credential and the model id are still the two things worth
#: checking.
PROVIDER_HINT_DEFAULT = (
    "Check the credential and the model id, or choose a different provider in the model control."
)

logger = logging.getLogger(__name__)


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
        spans: SpanRecorder | None = None,
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
        #: Where this turn's tool calls and proposals are recorded. Optional, so the loop's
        #: behaviour never depends on observability being installed.
        self._spans = spans

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
        except Exception as exc:
            # The model itself failed: an unreachable endpoint, a rejected key, a model id the
            # provider does not serve. Reported as a finished turn with a reason, never raised.
            #
            # Raised, it was a 500: the caller saw "Internal Server Error" with no explanation
            # while the log held an unhandled `APIConnectionError`. That is the worst of both -
            # nothing the user can act on, and nothing the response admits. The tool path already
            # works this way (see `_call`); this is the same rule applied to the other thing that
            # talks to the outside world.
            #
            # A broad catch is deliberate. Which exceptions a provider SDK raises is not a closed
            # set this loop can enumerate, and the alternative to catching them is that a network
            # blip ends a turn as an unhandled error. `str(exc)` is *not* in the response - see
            # `PROVIDER_FAILURE_TEMPLATE` for why - but it is in the log, at debug, where a
            # developer can read it without it being published.
            status_code = getattr(exc, "status_code", None)
            reason = type(exc).__name__
            if isinstance(status_code, int):
                reason = f"{reason} (HTTP {status_code})"
            logger.warning(
                "model provider failed during a turn",
                extra={"provider": self._provider.name, "reason": reason},
            )
            logger.debug("provider failure detail", exc_info=exc)
            hint = (
                PROVIDER_HINTS[status_code]
                if isinstance(status_code, int) and status_code in PROVIDER_HINTS
                else PROVIDER_HINT_DEFAULT
            )
            return TurnResult(
                text=PROVIDER_FAILURE_TEMPLATE.format(reason=reason, hint=hint),
                stop_reason="error",
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
            if self._spans is not None:
                # Recorded with the arguments, because the question a trace is opened to answer is
                # "what exactly was proposed" — and the row is the only other place those live.
                self._spans.add(
                    PROPOSAL,
                    approval.action,
                    approval_id=approval.id,
                    arguments=dict(approval.payload),
                    state=approval.state,
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

            outcome, duration_ms = await self._call_timed(call)
            records.append(
                ToolCallRecord(
                    call=call,
                    outcome="failed" if outcome.is_error else "executed",
                    # The short description when the executor gave one, and the model-facing text
                    # only as a fallback. This used to be thrown away entirely, which made a failed
                    # call indistinguishable from a successful one in the response — the only trace
                    # of the failure was the text fed to the model.
                    detail=outcome.summary or outcome.text,
                    duration_ms=duration_ms,
                )
            )
            messages.append(Message.tool_result(call, outcome.text))

    async def _call_timed(self, call: ToolCallRequest) -> tuple[ToolOutcome, int]:
        """Run one read and report how long it took.

        The timing is unconditional and the span is not, and they are separate on purpose: the
        response's ``duration_ms`` is part of what a caller sees, while a span is only recorded
        when a recorder was supplied. Returning both from one place means a call site cannot
        remember one and forget the other.

        The span wraps the call, so the tracer's own clock measures it — the same clock that
        timestamps every other span in the run. Measuring here with a second clock would make a
        trace's durations fail to add up, which is the one property a trace is read for.
        """
        started = time.monotonic()
        if self._spans is None:
            outcome = await self._call(call)
        else:
            with self._spans.span(TOOL_CALL, call.name, arguments=dict(call.arguments)) as span:
                outcome = await self._call(call)
                span.attributes["outcome"] = "failed" if outcome.is_error else "executed"
                span.attributes["is_error"] = outcome.is_error
        return outcome, int((time.monotonic() - started) * 1000)

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

    async def _call(self, call: ToolCallRequest) -> ToolOutcome:
        """Run one tool, turning a transport failure into something the model can read.

        The slice granted here is what makes the deadline bound a *single* call rather than only
        the gaps between them: without it, one tool that never returns would hold the turn open
        forever, since the next checkpoint is never reached. `Deadline.grant` raises when nothing
        is left, which the caller's `except` below reports as a failed call — and the turn ends at
        the following checkpoint with the honest reason.
        """
        granted = self._deadline.grant() if self._deadline is not None else None
        try:
            return await self._executor.call(call.name, dict(call.arguments), timeout_s=granted)
        except Exception as exc:
            # Deliberately broad. The alternative to catching here is a traceback, which ends
            # the turn with nothing the model can act on — and the failures worth catching are
            # exactly the ones the transport raises, which are not a closed set the loop can
            # enumerate. The message names the reason and tells the model not to retry
            # unchanged, because a model told only that something failed usually does.
            #
            # `is_error=True` because a call that raised did not produce an answer. Without it
            # the loop would record a call it never managed to make as having succeeded, which
            # is the one reading a caller must not be given.
            return ToolOutcome(
                text=TRANSPORT_FAILURE_TEMPLATE.format(reason=type(exc).__name__),
                is_error=True,
            )


def _declared_key(call: ToolCallRequest) -> str | None:
    """The ``client_request_id`` the model supplied, if it supplied a string.

    Passed through when present so the key is the model's own — which is what makes a retried
    proposal reuse it — and left to the repository to mint when absent. Not validated further
    here: the MCP server owns that contract, and duplicating its rules would give two answers.
    """
    declared = call.arguments.get("client_request_id")
    return declared if isinstance(declared, str) and declared else None
