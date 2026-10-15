"""Run every case against a live stack and write what happened.

Invoked by ``make eval`` (via ``python -m eval``). The contract it honours is the one an eval is
useless without: **it exits non-zero when a case fails**, so it can gate a change rather than only
describe one, and it writes both a machine-readable and a human-readable result so the two
audiences do not have to compromise.

The cases are performed in order against one running stack, which is deliberate. A fresh stack per
case would be simpler and would hide exactly the class of defect this suite exists for — state that
leaks between turns. The approval cases write real tickets and the memory cases accumulate real
facts, so a case that only passed because it started from nothing would look correct here and fail
in a conversation.

The one exception is a case that declares `stack` overrides. A turn budget and an approval TTL are
read when the application is built, so a case that needs a different one cannot be served by a
running process at all; it gets a stack of its own, started and stopped around it. That is a
narrower thing than "a clean stack for every case": it is a clean stack only for the cases whose
claim is about process configuration rather than about accumulated state.

Two things are recorded for every case that no assertion consumes, and they are worth keeping:
how long it took, and the raw payloads. The first is what tells a reviewer that a case is slow
enough to be worth watching; the second is what makes a failure diagnosable without re-running it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from eval.provider import EvalProvider, ProviderError, resolve_provider
from eval.report import render_markdown, summarise
from eval.scoring import (
    DECISION,
    INVOCATION,
    CaseResult,
    Observation,
    Step,
    UnknownAssertionError,
    evaluate,
)
from eval.stack import CASE_OVERRIDABLE_ENV, Stack, StackError, start_stack

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = Path(__file__).resolve().parent / "cases.json"
#: Inside the package, not at the repository root, because that is where the plan records the
#: deliverable (section 1.1 names `eval/results/latest.md`) and because the run output belongs
#: beside the cases that produced it. The README quotes this file, so a path that moved would
#: make a documented artefact unfindable.
DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"

#: The kinds of step a case may contain. Declared here and checked against, so a case file that
#: invents a step fails before two services are started rather than being skipped silently
#: half-way through a run. Same reasoning as the scorer's unknown-assertion error: a suite that
#: quietly measures less than it says is the one failure mode an eval must not have.
INVOKE_STEP = "invoke"
DECIDE_STEP = "decide"
#: Tear the agent down and build it again, against the same database. One step, no payload.
RESTART_STEP = "restart"
#: Wait, so a case can make time pass between two steps. Seconds, as a number.
SLEEP_STEP = "sleep_s"

STEP_KINDS: tuple[str, ...] = (INVOKE_STEP, DECIDE_STEP, RESTART_STEP, SLEEP_STEP)

#: The status a decision arrives as when the gate refuses it. 409 is "the request was fine, the
#: state prevents it" — which for this suite means the proposal expired before anyone answered.
REFUSED_STATUSES: tuple[int, ...] = (409,)

#: How a refused decision is named in a recorded step. The same word the loop uses for a call it
#: declines to make, and for the same reason: the important thing about it is that a write did not
#: happen and nothing broke, which neither "declined" nor "failed" says.
REFUSED = "refused"

#: A case that only a provider answering from rules can drive, declared in the case file.
#:
#: The one case that needs this asks the agent to send a value the backend's schema forbids, which
#: is how the *refusal* path stays reachable — and only a rule-driven provider can be told to do
#: that. A live model correctly declines to send an invalid priority, so the case fails against it
#: for behaving well. Reported as skipped rather than as a pass (which it did not earn) or a
#: failure (which would be noise): see the live `openai` run in `eval/results/live-openai/`.
DETERMINISTIC_ONLY = "deterministic_only"


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Read and shape-check the case file.

    Validated here rather than discovered one case at a time during a run, because a malformed
    case file should fail before the stack is started: spending a minute bringing up two services
    to then report a missing key is a poor trade.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = raw.get("cases") if isinstance(raw, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{path} has no non-empty 'cases' list")

    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise TypeError(f"case {index} is not an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"case {index} has no 'id'")
        if case_id in seen:
            raise ValueError(f"duplicate case id {case_id!r}; ids are how results are compared")
        seen.add(case_id)

        steps = case.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"case {case_id!r} has no steps")
        for step in steps:
            _check_step(case_id, step)
        if not isinstance(case.get("user_id"), str):
            raise TypeError(f"case {case_id!r} has no user_id; memory is scoped by one")

        _check_overrides(case_id, case.get("stack"))

        # Checked as a bool rather than for truthiness: a case file that wrote the *provider name*
        # here would be skipped for every provider, which is a silent way to stop measuring a case.
        if DETERMINISTIC_ONLY in case and not isinstance(case[DETERMINISTIC_ONLY], bool):
            raise TypeError(
                f"case {case_id!r} has a non-boolean {DETERMINISTIC_ONLY}: "
                f"{case[DETERMINISTIC_ONLY]!r}"
            )

        # The optional two-user setup, checked here for the same reason as everything else: a
        # malformed case should fail before two services are started, not half-way through a run.
        for key in ("second_user_id", "second_user_prompt"):
            if key in case and not isinstance(case[key], str):
                raise TypeError(f"case {case_id!r} has a non-string {key}")
        if ("second_user_id" in case) != ("second_user_prompt" in case):
            raise ValueError(
                f"case {case_id!r} sets one of second_user_id/second_user_prompt; both or neither"
            )

    return cases


def _check_step(case_id: str, step: Any) -> None:
    """Reject a step the runner would not know what to do with.

    Exactly one key per step, and it has to be one the runner performs. A step carrying two keys
    — an `invoke` that also said `restart` — would otherwise be ambiguous, and the runner would do
    whichever it happened to check first: a case measuring something other than what it says.
    """
    if not isinstance(step, dict):
        raise TypeError(f"case {case_id!r} has a step that is not an object")

    keys = set(step)
    if len(keys) != 1 or not keys <= set(STEP_KINDS):
        raise ValueError(
            f"case {case_id!r} has a step that is not exactly one of {STEP_KINDS}: {step!r}"
        )

    if RESTART_STEP in step and step[RESTART_STEP] is not True:
        raise TypeError(
            f"case {case_id!r} restarts with {step[RESTART_STEP]!r}; a restart is `true`"
        )

    if SLEEP_STEP in step:
        seconds = step[SLEEP_STEP]
        # `isinstance(True, int)` is true, so a bool has to be excluded explicitly: `true` as a
        # duration would sleep for a second and look like it worked.
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
            raise TypeError(
                f"case {case_id!r} sleeps for {seconds!r}, which is not a duration in seconds"
            )


def _check_overrides(case_id: str, overrides: Any) -> None:
    """Reject a `stack` block that asks for something the eval cannot vary.

    The keys are environment variable names, used verbatim rather than translated through a
    friendlier alias. A mapping layer would be one more place for the case file and the settings to
    disagree about which knob is which — and the whole point of an override is that it reaches the
    setting the case names.
    """
    if overrides is None:
        return
    if not isinstance(overrides, dict):
        raise TypeError(f"case {case_id!r} has a 'stack' that is not an object")

    unknown = sorted(set(overrides) - CASE_OVERRIDABLE_ENV)
    if unknown:
        raise ValueError(
            f"case {case_id!r} overrides {unknown}, which the eval does not vary. "
            f"Varyable: {sorted(CASE_OVERRIDABLE_ENV)}"
        )

    for key, value in overrides.items():
        if not isinstance(value, str):
            raise TypeError(f"case {case_id!r} sets {key} to {value!r}, which is not a string")


def _skip_reason(case: dict[str, Any], provider: EvalProvider) -> str | None:
    """Why this case will not run against this provider, or ``None`` to run it.

    A string rather than a bool, because the reason ends up in the result file: "skipped" on its
    own invites the reader to assume the case is broken, and the sentence says instead that the
    case cannot be driven this way.
    """
    if case.get(DETERMINISTIC_ONLY) and provider.is_live:
        return (
            "needs a provider that answers from rules: the case asks the agent to send a value "
            "the backend rejects, which a live model correctly refuses to do"
        )
    return None


def _require_previous_approval(index: int, decide: Any, pending_approval_id: str | None) -> str:
    """The approval id a decision must be applied to, or a failure explaining the case.

    A helper rather than an inline `raise` so the check does not sit inside the case's `try`
    block, where a defect in the *case file* would be caught by the handler that reports a
    failure of the *agent* and be described as one. The two are different, and the message here
    is what keeps them apart.
    """
    if not isinstance(decide, dict):
        raise TypeError(f"step {index} has a 'decide' that is not an object")
    if str(decide.get("approval")) != "previous" or not pending_approval_id:
        raise StackError(
            f"step {index} decides on a previous approval, but step {index - 1} proposed "
            "none — the case describes a sequence the agent did not produce"
        )
    return pending_approval_id


async def run_case(stack: Stack, case: dict[str, Any]) -> CaseResult:
    """Perform one case and score it.

    The ticket counts bracket the case and, when there is a decision, the moment before it. That
    third reading is what lets a case state the approval requirement exactly — the proposing turn
    wrote nothing, and the approving decision wrote one — instead of asserting only on the total.
    """
    result = CaseResult(
        case_id=str(case["id"]),
        family=str(case.get("family", "")),
        why=str(case.get("why", "")),
    )
    steps: list[Step] = []
    session_id: str | None = None
    pending_approval_id: str | None = None
    tickets_before_decision: int | None = None
    tickets_before = stack.mock.ticket_count
    # Seeded before the first step, and overwritten with the finished readings on the success path
    # below. A case that aborts part-way — which is the shape *every* failure in the live `openai`
    # run took — otherwise records an error and no evidence at all, and the evidence is the only
    # thing that says whether the agent answered wrongly or was never asked.
    result.tickets = {"before": tickets_before, "before_decision": None, "after": None}

    if case.get("chaos"):
        stack.mock.set_chaos(enabled=True, rate=1.0)

    try:
        # Setup for a two-user case: the *other* person states their facts, in their own session.
        #
        # Deliberately not recorded as a step. `payloads_avoid` scans every recorded payload, and
        # the whole point of the isolation case is that one user's words appear in no response —
        # so recording the turn that legitimately contains them would make the case fail on its
        # own setup rather than on the answer it is testing.
        await _seed_other_user(stack, case)

        for index, step in enumerate(case["steps"]):
            if "invoke" in step:
                payload = await stack.invoke(
                    prompt=str(step["invoke"]), user_id=str(case["user_id"]), session_id=session_id
                )
                # Carried to the next step so a decision lands on the same conversation, and so a
                # multi-turn case is a conversation rather than several unrelated ones.
                session_id = str(payload.get("session_id") or session_id or "")
                approval = payload.get("pending_approval")
                if isinstance(approval, dict):
                    pending_approval_id = str(approval.get("approval_id") or "")
                steps.append(Step(index=index, kind=INVOCATION, payload=payload))
                # Recorded here rather than only at the end: `result.steps` is what a reader of
                # the JSON has when a later step fails, and a failing case is exactly the one whose
                # earlier payloads need reading.
                result.steps = tuple(steps)
                continue

            if RESTART_STEP in step:
                # Not recorded as a step. It produces no response to assert on, and recording it
                # would shift every `turn` index in the case — so a case that restarts between a
                # proposal and a decision would have to count the restart when naming the turn it
                # means, which is a trap for whoever writes the next one.
                stack.restart_agent()
                continue

            if SLEEP_STEP in step:
                # Not recorded either, and for the same reason: the wait is setup, and the thing
                # asserted is what the *next* step finds rather than that time passed.
                await asyncio.sleep(float(step[SLEEP_STEP]))
                continue

            decide = step["decide"]
            target = _require_previous_approval(index, decide, pending_approval_id)
            expect = str(decide.get("expect", "")).strip()
            if tickets_before_decision is None:
                tickets_before_decision = stack.mock.ticket_count
            payload = await stack.decide(
                approval_id=target,
                user_id=str(case["user_id"]),
                approved=bool(decide.get("approved")),
                # Only a decision that declares it expects a refusal is allowed to receive one.
                # Declared in the case rather than inferred from the status, so a 409 arriving in
                # a case that expected a decision is still a failure — and any other status is a
                # failure whether or not a refusal was expected.
                tolerate=REFUSED_STATUSES if expect == REFUSED else (),
            )
            if expect == REFUSED:
                # Named in the same vocabulary as the loop's outcomes, so `decision_outcome_is`
                # reads it without a second assertion type. The status is carried beside it as the
                # evidence for the word.
                payload = {"outcome": REFUSED, **payload}
            # A decision resolves the proposal it names; a second one would be refused, so the id
            # is cleared rather than left to be decided twice by a mistaken case.
            pending_approval_id = None
            steps.append(Step(index=index, kind=DECISION, payload=payload))
            result.steps = tuple(steps)

        observation = Observation(
            steps=tuple(steps),
            tickets_before=tickets_before,
            tickets_after=stack.mock.ticket_count,
            tickets_before_decision=tickets_before_decision,
        )
        # Kept on the result so the committed JSON explains a failure on its own. Assigned before
        # scoring so a case that cannot be scored still records what it produced.
        result.steps = tuple(steps)
        result.tickets = {
            "before": tickets_before,
            "before_decision": tickets_before_decision,
            "after": observation.tickets_after,
        }
        try:
            result.checks = evaluate(case, observation)
        except (UnknownAssertionError, IndexError) as exc:
            # A case the scorer cannot interpret is a broken case, not a failing agent, and the
            # distinction has to survive into the result file.
            result.error = f"the case could not be scored: {exc}"
    except (StackError, httpx.HTTPError) as exc:
        # One catch rather than two: to a reader of the result file, "the agent answered 500" and
        # "the agent never answered" are both "this case did not complete", and the sentence in
        # `error` says which.
        result.error = str(exc)
    finally:
        # Switched off here and not only before the next case, so a failure half-way through the
        # failure case cannot leave the mock broken for everything after it.
        if case.get("chaos"):
            stack.mock.set_chaos(enabled=False)

    return result


async def _seed_other_user(stack: Stack, case: dict[str, Any]) -> None:
    """Have a second user state a fact, so a case can prove it does not leak.

    A no-op for a case that does not declare one.
    """
    other_user = case.get("second_user_id")
    prompt = case.get("second_user_prompt")
    if not isinstance(other_user, str) or not isinstance(prompt, str):
        return
    await stack.invoke(prompt=prompt, user_id=other_user)


def _result_payload(
    results: list[CaseResult], *, duration_s: float, provider: EvalProvider
) -> dict[str, Any]:
    """The JSON form, shaped so a diff between two runs is readable.

    The provider goes in the file rather than only on the terminal, and that is the point of
    committing a run: "22 of 22 passed" means one thing when the model was `scripted` — the
    orchestration is sound — and something different when it was a live model. A result file that
    did not say which would be cited as evidence for the wrong claim.
    """
    return {
        "version": 1,
        "duration_s": round(duration_s, 2),
        "provider": {"name": provider.name, "live": provider.is_live},
        "passed": sum(1 for result in results if result.passed),
        "failed": sum(1 for result in results if result.failed),
        # Counted separately, and the sum of the three is the number of cases. Folding a skip into
        # either of the others would make the file's own arithmetic a lie: 21 passed + 1 failed
        # would be 22 cases when one of them never ran.
        "skipped": sum(1 for result in results if result.skipped is not None),
        "cases": [
            {
                "id": result.case_id,
                "family": result.family,
                "passed": result.passed,
                "error": result.error,
                "checks": [
                    {
                        "assertion": check.assertion,
                        "ok": check.ok,
                        "detail": check.detail,
                    }
                    for check in result.checks
                ],
                # The responses themselves. Recorded because a failure that can only be
                # understood by re-running the suite is one nobody investigates, and because
                # the whole point of the suite is that what the service said is evidence.
                "skipped": result.skipped,
                "tickets": result.tickets,
                "steps": [
                    {"index": step.index, "kind": step.kind, "payload": step.payload}
                    for step in result.steps
                ],
            }
            for result in results
        ],
    }


async def _run_on_own_stack(
    case: dict[str, Any], overrides: Mapping[str, str], provider: EvalProvider
) -> CaseResult:
    """Run one case against a stack built for it, and take that stack down afterwards.

    The teardown is in a `finally` so a case that raises still gives its ports and its MCP
    subprocess back. A leak here would not be noticed until the suite had run often enough to
    exhaust something, which is the kind of failure nobody traces back to the eval.

    The run's provider is passed here as well as to the shared stack. Missing it would mean a case
    that varies process configuration ran on `scripted` while the rest of the suite ran on a model
    — a suite that measured two different things and reported one number.
    """
    stack = start_stack(overrides=overrides, extra_env=provider.env)
    try:
        return await run_case(stack, case)
    finally:
        stack.shutdown()


async def run(
    cases: list[dict[str, Any]], *, verbose: bool, provider: EvalProvider
) -> tuple[list[CaseResult], float]:
    """Bring up the shared stack, run every case, and take everything down again.

    A case that declares `stack` overrides gets its own stack, because the two settings a case has
    needed so far are read when the application is built rather than per request. Everything else
    runs against the one shared stack — deliberately, since a case that passed only because it
    started from a clean process would hide the state-leak defect this suite exists for.
    """
    started = time.monotonic()
    stack = start_stack(extra_env=provider.env)
    results: list[CaseResult] = []

    try:
        for case in cases:
            case_started = time.monotonic()
            # Decided before any work: a skipped case must not start a stack of its own, and must
            # not touch the shared one — a case that declared itself out and then left a proposal
            # behind would corrupt the cases that follow it.
            skip = _skip_reason(case, provider)
            if skip is not None:
                results.append(
                    CaseResult(
                        case_id=str(case["id"]),
                        family=str(case.get("family", "")),
                        why=str(case.get("why", "")),
                        skipped=skip,
                    )
                )
                print(f"  SKIP  {case['id']}  ({skip})")
                continue

            overrides = case.get("stack")
            if overrides:
                result = await _run_on_own_stack(case, overrides, provider)
            else:
                result = await run_case(stack, case)
            elapsed = time.monotonic() - case_started
            results.append(result)

            verdict = "FAIL" if result.failed else "PASS"
            print(f"  {verdict}  {result.case_id}  ({elapsed:.1f}s)")
            if not result.passed and verbose:
                if result.error:
                    print(f"        {result.error}")
                for failure in result.failures:
                    print(f"        {failure.detail}")
    finally:
        stack.shutdown()

    return results, time.monotonic() - started


def _select(cases: list[dict[str, Any]], only: str | None) -> list[dict[str, Any]]:
    if not only:
        return cases
    chosen = [case for case in cases if only.lower() in str(case["id"]).lower()]
    if not chosen:
        raise SystemExit(f"no case id contains {only!r}")
    return chosen


def main(argv: list[str] | None = None) -> int:
    """The entry point ``python -m eval`` calls. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m eval",
        description="Run the behavioural eval suite against a live agent.",
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--only", default=None, help="run only cases whose id contains this")
    parser.add_argument("--quiet", action="store_true", help="print the table only")
    args = parser.parse_args(argv)

    cases = _select(load_cases(args.cases), args.only)

    try:
        provider = resolve_provider()
    except ProviderError as error:
        # Refused before a stack is built, so a mistyped provider name or a missing key costs a
        # sentence rather than a minute of bringing two services up to fail inside a case.
        print(f"cannot run the suite: {error}", file=sys.stderr)
        return 2

    print(f"Running {len(cases)} case(s) against a live stack.")
    print(f"Provider: {provider.describe()}\n")
    if provider.is_live:
        print(
            "  A live model is not deterministic: a case asserting which tool was chosen can "
            "fail because the model chose differently, not because the code regressed. Read the "
            "failures as findings, not as a broken build.\n"
        )

    results, duration_s = asyncio.run(run(cases, verbose=not args.quiet, provider=provider))
    markdown = render_markdown(
        results,
        duration_s=duration_s,
        passed=summarise(results),
        provider_name=provider.name,
        provider_is_live=provider.is_live,
    )

    args.results.mkdir(parents=True, exist_ok=True)
    (args.results / "latest.json").write_text(
        json.dumps(_result_payload(results, duration_s=duration_s, provider=provider), indent=2)
        + "\n",
        encoding="utf-8",
    )
    (args.results / "latest.md").write_text(markdown + "\n", encoding="utf-8")

    print()
    print(markdown)
    print()
    print(f"Wrote {args.results / 'latest.md'} and {args.results / 'latest.json'}")

    # Counted on `failed`, not on `not passed`: a skipped case did not fail, and letting it turn
    # the exit code red would make every live run fail for a case it deliberately did not run.
    failed = [result for result in results if result.failed]
    if failed:
        print(f"\n{len(failed)} case(s) failed.")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through `python -m eval`
    raise SystemExit(main())
