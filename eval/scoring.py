"""What a case asserts, and how a failure is explained.

Every assertion here is about *observed behaviour*, never about the shape of the code. That is a
deliberate restriction: an eval that asserts "the handler called `search_tickets`" restates the
implementation and breaks whenever it is refactored, while an eval that asserts "a search tool was
called and no ticket was created" describes the requirement and survives the rewrite.

Assertions are declared in ``cases.json`` as plain objects — ``{"type": "tool_called", "name":
"search_tickets"}`` — rather than as code, so the case file is the whole specification of what is
being measured. A reviewer can read it without reading Python, and adding a case does not mean
touching this module.

**The failure messages are the product.** An eval whose only output is a red cross tells you a case
broke; one that says "expected no create_ticket call in step 0, but these calls were made:
create_ticket(title=…)" tells you what broke. Every check below therefore builds a sentence that
names what was wanted and what actually happened, using the response's own values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: The kinds of step a case can contain. Mirrors the two the runner knows how to perform.
INVOCATION = "invocation"
DECISION = "decision"


@dataclass(frozen=True)
class Step:
    """One thing the runner did, and what came back."""

    index: int
    kind: str
    payload: dict[str, Any]

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        """The tool calls this step recorded, or none for a step that cannot make them."""
        calls = self.payload.get("tool_calls")
        return calls if isinstance(calls, list) else []

    @property
    def stop_reason(self) -> str:
        return str(self.payload.get("stop_reason", ""))

    @property
    def has_pending_approval(self) -> bool:
        return self.payload.get("pending_approval") is not None


@dataclass(frozen=True)
class Observation:
    """Everything one case produced, in the vocabulary the assertions speak.

    The ticket counts are carried alongside the steps because the strongest claim a case can make
    about a write is not what the agent said it did, but whether the backend changed. ``before``
    and ``after`` bracket the whole case; ``before_decision`` is what makes it possible to say
    that the *proposing* step wrote nothing, which is the approval requirement stated exactly.
    """

    steps: tuple[Step, ...]
    tickets_before: int
    tickets_after: int
    tickets_before_decision: int | None = None


@dataclass(frozen=True)
class Check:
    """One assertion's verdict, with the sentence a reader needs."""

    assertion: dict[str, Any]
    ok: bool
    detail: str


@dataclass
class CaseResult:
    """One case's outcome."""

    case_id: str
    family: str
    why: str
    checks: list[Check] = field(default_factory=list)
    error: str | None = None
    #: Every step's raw payload. No assertion reads it, and it is kept anyway: a failure that can
    #: only be understood by re-running the suite is a failure that gets skipped, and the payload
    #: is what turns "expected 1 ticket, gained 0" into something a reader can act on.
    steps: tuple[Step, ...] = ()
    #: The backend readings the write assertions were scored against. Recorded for the same
    #: reason as the payloads: "gained 0 ticket(s)" is only checkable if the three counts that
    #: produced it are visible, and a count is the one piece of evidence a payload cannot carry.
    tickets: dict[str, int | None] = field(default_factory=dict)

    #: Why this case was not run, or ``None`` when it was. Set by the runner for a case that
    #: declares it needs a provider this run is not using — see `deterministic_only` in
    #: `cases.json`.
    skipped: str | None = None

    @property
    def failed(self) -> bool:
        """Whether the agent did something wrong, as opposed to not being asked.

        Separate from `passed` because a skipped case is neither. Collapsing the two would make a
        skip either a lie (reported as a pass it did not earn) or noise (a red cell for a case that
        never ran), and the report's whole value is that a green cell means something.
        """
        return self.error is not None or any(not check.ok for check in self.checks)

    @property
    def passed(self) -> bool:
        return not self.failed and self.skipped is None

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.ok]


class UnknownAssertionError(ValueError):
    """A case asked for an assertion this module does not implement.

    Raised rather than skipped. A typo in a case file that quietly asserted nothing would make the
    suite greener and less true, which is the one failure mode an eval must never have.
    """


def _describe(value: str) -> str:
    """A quoted value, so an empty string is visible in a failure message."""
    return f'"{value}"'


def _select(steps: tuple[Step, ...], turn: int | None) -> tuple[Step, ...]:
    """The steps an assertion applies to.

    With a ``turn``, that step alone — which is how a case talks about a specific phase of a
    sequence, such as the turn that proposed a write. Without one, every *invocation* step: a
    decision has no stop reason and no tool calls, so including it would make "the run completed"
    unanswerable in any case that also decides something.
    """
    if turn is not None:
        if turn < 0 or turn >= len(steps):
            raise IndexError(f"the case has no step {turn}; it has {len(steps)}")
        return (steps[turn],)
    return tuple(step for step in steps if step.kind == INVOCATION)


def _calls(steps: tuple[Step, ...]) -> list[dict[str, Any]]:
    return [call for step in steps for call in step.tool_calls]


def _call_names(calls: list[dict[str, Any]]) -> str:
    return ", ".join(repr(call.get("name")) for call in calls) or "no calls"


def _evaluate_one(assertion: dict[str, Any], observation: Observation) -> Check:
    """Dispatch one assertion. Every branch returns a verdict, or raises for an unknown type."""
    kind = str(assertion.get("type", ""))
    turn = assertion.get("turn")
    if turn is not None and not isinstance(turn, int):
        raise UnknownAssertionError(f"'turn' must be an integer, got {turn!r}")

    steps = _select(observation.steps, turn)

    if kind == "stop_reason_is":
        wanted_reason = str(assertion["value"])
        seen_reasons = [step.stop_reason for step in steps]
        ok = bool(seen_reasons) and all(reason == wanted_reason for reason in seen_reasons)
        return Check(
            assertion,
            ok,
            f"expected stop_reason {_describe(wanted_reason)}, saw {seen_reasons}",
        )

    if kind == "approval_pending":
        ok = bool(steps) and all(step.has_pending_approval for step in steps)
        pending_flags = [step.has_pending_approval for step in steps]
        return Check(assertion, ok, f"expected a pending approval, saw pending={pending_flags}")

    if kind == "no_tool_calls":
        calls = _calls(steps)
        return Check(
            assertion,
            not calls,
            f"expected no tool calls, saw {_call_names(calls)}",
        )

    if kind == "tool_called":
        name = str(assertion["name"])
        calls = _calls(steps)
        ok = any(call.get("name") == name for call in calls)
        return Check(assertion, ok, f"expected a {name} call, saw {_call_names(calls)}")

    if kind == "tool_not_called":
        name = str(assertion["name"])
        calls = _calls(steps)
        offenders = [call for call in calls if call.get("name") == name]
        return Check(
            assertion,
            not offenders,
            f"expected no {name} call, saw {_call_names(offenders)}",
        )

    if kind == "tool_not_executed":
        # The assertion the approval requirement actually needs. A proposed write *is* recorded —
        # deliberately, so "I did not try" stays distinguishable from "I tried and a human has to
        # say yes" — which means `tool_not_called` is the wrong question to ask about it. What
        # must be true is that the write did not *run*.
        name = str(assertion["name"])
        calls = [call for call in _calls(steps) if call.get("name") == name]
        executed = [call for call in calls if call.get("outcome") == "executed"]
        return Check(
            assertion,
            bool(calls) and not executed,
            f"expected {name} not to have run, saw outcomes "
            f"{[call.get('outcome') for call in calls]}",
        )

    if kind == "tool_error":
        name = str(assertion["name"])
        calls = [call for call in _calls(steps) if call.get("name") == name]
        ok = any(call.get("is_error") for call in calls)
        return Check(
            assertion,
            ok,
            f"expected a failed {name} call, saw {[call.get('is_error') for call in calls]}",
        )

    if kind == "tool_argument_is":
        name = str(assertion["name"])
        key = str(assertion["key"])
        wanted_value = assertion["value"]
        calls = [call for call in _calls(steps) if call.get("name") == name]
        seen_values = [call.get("arguments", {}).get(key) for call in calls]
        return Check(
            assertion,
            any(value == wanted_value for value in seen_values),
            f"expected {name} to be called with {key}={wanted_value!r}, saw {seen_values}",
        )

    if kind == "output_contains":
        wanted_text = str(assertion["value"])
        outputs = [str(step.payload.get("output", "")) for step in steps]
        return Check(
            assertion,
            any(wanted_text in output for output in outputs),
            f"expected an output containing {_describe(wanted_text)}, saw {outputs}",
        )

    if kind == "output_is_not_empty":
        # The claim is "it answered", not "it said these words". A case that pins a phrase pins a
        # *provider*: the deterministic provider's fallback happens to begin with "Understood", and
        # a live model refuses a poem request in whatever words it chooses — so `output_contains
        # "Understood"` failed a run that behaved correctly, which is exactly what the first live
        # `openai` run of this suite did. The requirement being measured here is that the agent
        # said something rather than nothing, and that is what this asserts.
        outputs = [str(step.payload.get("output", "")) for step in steps]
        return Check(
            assertion,
            bool(outputs) and all(output.strip() for output in outputs),
            f"expected every step to answer with something, saw "
            f"{[output[:80] for output in outputs]}",
        )

    if kind == "payloads_avoid":
        # Scans the whole recorded payload, not one field. The claim is "this string appears
        # nowhere the service said anything", and a check that inspected only `output` would pass
        # while the same string sat in a tool argument, a trace field or a pending approval.
        forbidden = str(assertion["value"])
        blobs = [str(step.payload) for step in observation.steps]
        # Named distinctly for the same reason the comment below gives about `wanted`/`seen`: this
        # function already binds `offenders` to a list of *calls*, and rebinding the same name to
        # a list of *indices* is the kind of thing mypy catches and a reader does not — the second
        # assignment was a type error, not just a style choice.
        offending_steps = [index for index, blob in enumerate(blobs) if forbidden in blob]
        return Check(
            assertion,
            not offending_steps,
            f"expected {_describe(forbidden)} in no payload, found it in step(s) {offending_steps}",
        )

    if kind == "decision_outcome_is":
        wanted_outcome = str(assertion["value"])
        outcomes = [
            str(step.payload.get("outcome", ""))
            for step in observation.steps
            if step.kind == DECISION
        ]
        return Check(
            assertion,
            wanted_outcome in outcomes,
            f"expected a decision outcome of {_describe(wanted_outcome)}, saw {outcomes}",
        )

    # Each branch below names its locals distinctly rather than reusing `wanted`/`seen`. Those
    # two read well in any one branch and badly across the function: a reader has to check which
    # branch they are in to know whether `seen` is a list of reasons, of flags, or of counts.
    if kind == "tickets_delta":
        wanted_gain = int(assertion["value"])
        seen_gain = observation.tickets_after - observation.tickets_before
        return Check(
            assertion,
            seen_gain == wanted_gain,
            f"expected the backend to gain {wanted_gain} ticket(s), it gained {seen_gain}",
        )

    if kind == "tickets_delta_before_decision":
        wanted_writes = int(assertion["value"])
        if observation.tickets_before_decision is None:
            raise UnknownAssertionError(
                "tickets_delta_before_decision needs a case with a decision step"
            )
        seen_writes = observation.tickets_before_decision - observation.tickets_before
        return Check(
            assertion,
            seen_writes == wanted_writes,
            f"expected the proposal to write {wanted_writes} ticket(s), it wrote {seen_writes}",
        )

    raise UnknownAssertionError(f"unknown assertion type: {kind!r}")


def evaluate(case: dict[str, Any], observation: Observation) -> list[Check]:
    """Every check in one case, in the order the case declared them.

    A failing check does not stop the ones after it. An eval run should tell you everything that
    is broken in one pass rather than the first thing, and the cost of continuing is one more
    comparison.
    """
    assertions = case.get("assertions", [])
    if not isinstance(assertions, list):
        raise UnknownAssertionError(f"assertions must be a list, got {type(assertions).__name__}")
    return [_evaluate_one(assertion, observation) for assertion in assertions]


#: Every assertion type this module implements. Used by `tests/unit/test_eval_cases.py` to prove
#: that no case in `cases.json` asks for something that would raise at run time, which is the one
#: way an eval silently measures less than it claims.
SUPPORTED_ASSERTIONS = frozenset(
    {
        "stop_reason_is",
        "approval_pending",
        "no_tool_calls",
        "tool_called",
        "tool_not_called",
        "tool_not_executed",
        "tool_error",
        "tool_argument_is",
        "output_contains",
        "output_is_not_empty",
        "payloads_avoid",
        "decision_outcome_is",
        "tickets_delta",
        "tickets_delta_before_decision",
    }
)
