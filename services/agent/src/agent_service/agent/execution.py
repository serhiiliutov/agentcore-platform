"""Executing an approved write. The only code in the service that can.

The loop proposes; this performs. Keeping them in separate files is the point rather than
tidiness: there is one function in this codebase that can cause a ticket to be created, it is
this one, and it cannot be reached without an approved approval row. A reader asking "where
could a write happen?" has one file to read.

Four properties, each of which is a specific way this could be wrong:

**The payload is the stored one.** ``approval.payload`` is read from the row and passed to the
tool. It is never re-derived from the model, never merged with newer arguments, and never
re-checked against a fresh schema. That is what makes "approve a ticket about the printer, get
a ticket about the printer" true even if the model is redeployed, replaced, or asked the same
question in a different conversation.

**The idempotency key is the row's.** ``approval.client_request_id`` — minted at *proposal*
time — is what is sent. Re-executing after a crash therefore reuses it, and Stage 4's ledger
answers ``replayed`` instead of creating a second ticket. A fresh key here would pass every
test that executes once and would silently duplicate on the one path that matters.

**A refusal is a state, not an exception.** A tool that fails marks the row ``failed`` with the
message and returns normally. The row is the record; a caller handed a traceback could not tell
"the backend refused" from "the process died", and only the first is recoverable. Note this is
the opposite of the loop's choice to swallow a transport failure — there, nothing needed
recording; here, the row would otherwise stay ``approved`` forever and be re-attempted on every
sweep.

**A pending or declined row cannot be executed at all.** That check lives in the repository
(``claim_for_execution``) rather than here, so it applies to every caller rather than to the
ones that remember it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from agent_service.db.models.approval import Approval, ApprovalState
from agent_service.db.repositories.approvals import ApprovalRepository


class ToolCallFailedError(RuntimeError):
    """A tool call completed, and the backend answered with a failure.

    Distinct from a transport exception, and the distinction is worth keeping: a call that never
    arrived and a call that arrived and was refused are different facts about the world. What they
    share is that the write did not happen, which is the only thing the writer acts on — so both
    reach it as an exception rather than as text it has to interpret.
    """


class WriteToolExecutor(Protocol):
    """What the writer needs from a tool executor, which is narrower than the loop's.

    The loop wants a sentence it can show the model, failures included, so its ``ToolExecutor``
    returns ``str``. The writer must not be able to mistake a failure for a success, and a ``str``
    return makes that mistake possible — which is not hypothetical: this project had a
    ``McpToolExecutor.call`` that rendered a failure as the text ``ERROR: ...``, and a writer that
    recorded the resulting ticket as ``executed`` because it had nothing else to go on.

    So the writer asks for a call that raises instead. The two readers of one result then cannot
    disagree about whether it succeeded, because only one of them is given a value to compare.
    """

    async def call_checked(self, name: str, arguments: dict[str, Any]) -> str:
        """Call a tool and return its text, raising `ToolCallFailedError` if it failed."""
        ...


#: Substrings by which Stage 5's MCP server signals that a write was not performed afresh.
#: Read from its own text rather than re-derived, because the server knows first-hand whether
#: it created anything and this code does not.
REPLAY_MARKERS = ("replayed", "recovered")


@dataclass(frozen=True)
class ExecutionResult:
    """What came of attempting an approved write.

    ``outcome`` is ``executed`` or ``failed``. ``replayed`` is carried separately even though
    the row state is ``executed``: the write reached the backend and was answered with "already
    done", which is a success whose *report* must not claim a fresh ticket was created.
    """

    approval_id: str
    action: str
    outcome: str
    detail: str = ""
    result: Mapping[str, Any] | None = None
    replayed: bool = False

    @property
    def succeeded(self) -> bool:
        """Whether the write completed — including a replay, which is a completed write."""
        return self.outcome == "executed"


class WriteExecutor:
    """Carries out an approved write, once."""

    def __init__(self, *, executor: WriteToolExecutor, approvals: ApprovalRepository) -> None:
        self._executor = executor
        self._approvals = approvals

    async def execute(self, approval_id: str, *, user_id: str) -> ExecutionResult:
        """Run the approved write named by ``approval_id``.

        Raises whatever ``claim_for_execution`` raises for a row that is not approved, because
        that is a caller mistake rather than an outcome of the write, and the two must never be
        reported the same way.
        """
        approval = self._approvals.claim_for_execution(approval_id, user_id=user_id)

        # A failure is caught here and becomes a state, per the module docstring. Note that this
        # covers BOTH an exception and a reported failure, because ``call_checked`` raises the
        # latter — which is what stopped a refused write from being recorded as executed.
        try:
            text = await self._executor.call_checked(approval.action, self._arguments(approval))
        except Exception as exc:
            return self._fail(approval, f"{type(exc).__name__}: {exc}")

        return self._finish(approval, text)

    def _arguments(self, approval: Approval) -> dict[str, Any]:
        """The arguments to send: the stored payload, with the stored key.

        The key is written in from the row rather than trusted to be in the payload already.
        The payload came from a model, and a model that omitted the key — or supplied a
        different one on a retry — would break the replay guarantee. The row is the authority
        on which key this approval is for.
        """
        arguments = dict(approval.payload)
        arguments["client_request_id"] = approval.client_request_id
        return arguments

    def _finish(self, approval: Approval, text: str) -> ExecutionResult:
        """Record success, and report whether the backend replayed instead of creating."""
        replayed = any(marker in text.lower() for marker in REPLAY_MARKERS)
        result: dict[str, Any] = {"text": text, "replayed": replayed}

        self._approvals.mark_executed(approval.id, result=result)

        if replayed:
            return ExecutionResult(
                approval_id=approval.id,
                action=approval.action,
                outcome="executed",
                detail=(
                    "the backend reported this was already done, so no second ticket was created"
                ),
                result=result,
                replayed=True,
            )

        return ExecutionResult(
            approval_id=approval.id,
            action=approval.action,
            outcome="executed",
            detail=text,
            result=result,
        )

    def _fail(self, approval: Approval, error: str) -> ExecutionResult:
        """Record a refusal by the backend and report it as an outcome, not a raise."""
        self._approvals.mark_failed(approval.id, error=error)
        return ExecutionResult(
            approval_id=approval.id,
            action=approval.action,
            outcome="failed",
            detail=error,
            replayed=False,
        )


def is_runnable(approval: Approval) -> bool:
    """Whether this approval is currently executable.

    Reads the model's own state values rather than restating them, so a state added to the
    lifecycle cannot leave this function behind. Used by the web UI to decide whether to offer
    an execute button, and by the gate routes to decide whether a decision was the last step.
    """
    return approval.state == ApprovalState.APPROVED.value
