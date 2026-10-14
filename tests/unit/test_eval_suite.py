"""The eval suite's own tests: that it measures what it claims, and can fail.

An eval suite is the one artefact whose failures nobody witnesses. A case that silently asserts
nothing still reports "pass", and the report is committed to the repository as evidence — so a
green suite that measures less than it says is worse than no suite at all.

Three things are checked here, in the order they could go wrong:

1. **The case file is well formed.** Every assertion names a type the scorer implements, every
   case has the fields the runner needs, and the duplicate/malformed cases the loader is supposed
   to reject are rejected.
2. **Scoring detects contradiction.** The `deliberately-broken.json` fixture is scored against an
   observation from a *successful* run, and the checks that should fail must fail. This is the
   unit-level half of the definition-of-done item that asks for a case proven able to fail a run;
   the end-to-end half is the recorded run in `eval/results/latest.md`.
3. **The report says what happened.** A failing case appears under Failures with its reason for
   existing; a passing run says "None."
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from eval.report import render_markdown, summarise
from eval.runner import load_cases
from eval.scoring import (
    DECISION,
    INVOCATION,
    SUPPORTED_ASSERTIONS,
    CaseResult,
    Observation,
    Step,
    UnknownAssertionError,
    evaluate,
)
from eval.stack import CASE_OVERRIDABLE_ENV

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES = REPO_ROOT / "eval" / "cases.json"
BROKEN = REPO_ROOT / "eval" / "fixtures" / "deliberately-broken.json"


# --- 1. the case file ---------------------------------------------------------


def test_every_assertion_in_every_case_is_implemented() -> None:
    """A typo in an assertion name raises when the case runs, half an hour into a suite.

    Checked here instead, so it fails in a second and names the case. The scorer raises rather
    than skips precisely so this cannot be swallowed — this test is the part that makes it
    arrive early.
    """
    for case in load_cases(CASES):
        for assertion in case["assertions"]:
            assert assertion.get("type") in SUPPORTED_ASSERTIONS, (
                f"case {case['id']!r} asks for {assertion.get('type')!r}, "
                f"which eval/scoring.py does not implement"
            )


def test_every_case_explains_itself() -> None:
    """`why` is not decoration: it is reproduced beside a failure, and a red cell without one
    is a red cell a reader has to go and research."""
    for case in load_cases(CASES):
        assert case["why"].strip(), f"case {case['id']!r} has no 'why'"
        assert case["family"].strip(), f"case {case['id']!r} has no 'family'"


def test_the_case_ids_are_unique() -> None:
    """Results are compared between commits by id, so a duplicate would silently shadow."""
    ids = [case["id"] for case in load_cases(CASES)]
    assert len(ids) == len(set(ids))


def test_the_deterministic_families_the_assignment_asks_for_are_present() -> None:
    """The claim the README makes about coverage, asserted rather than assumed."""
    cases = load_cases(CASES)
    families = " ".join(str(case["family"]) for case in cases).lower()
    for wanted in ("right tool chosen", "approval step respected", "out-of-scope", "failures"):
        assert wanted in families, f"no case covers {wanted!r}"


# --- 2. scoring detects contradiction ----------------------------------------


def _successful_search_run() -> Observation:
    """What the agent actually does for 'find tickets about the printer'.

    Taken from a real run's payload shape: one search call, executed, nothing created.
    """
    return Observation(
        steps=(
            Step(
                index=0,
                kind=INVOCATION,
                payload={
                    "output": "Done. I used 1 tool call(s) to answer.",
                    "stop_reason": "completed",
                    "tool_calls": [
                        {
                            "name": "search_tickets",
                            "arguments": {"q": "printer"},
                            "outcome": "executed",
                            "is_error": False,
                        }
                    ],
                    "pending_approval": None,
                },
            ),
        ),
        tickets_before=7,
        tickets_after=7,
    )


def test_the_deliberately_broken_case_fails_against_a_successful_run() -> None:
    """The definition-of-done claim, at unit level.

    The fixture asserts a `create_ticket` call and a ticket that was never created. Scored against
    the run above, exactly those two checks must fail — and the one that *is* true must pass, which
    is what proves the scorer is scoring rather than rejecting the whole case.
    """
    case = load_cases(BROKEN)[0]

    checks = evaluate(case, _successful_search_run())

    assert [check.ok for check in checks] == [True, False, False]
    messages = " ".join(check.detail for check in checks if not check.ok)
    assert "create_ticket" in messages
    assert "gained 0" in messages


def test_a_wrong_count_is_caught() -> None:
    assertion = {"type": "tickets_delta", "value": 1}

    check = evaluate({"assertions": [assertion]}, _successful_search_run())[0]

    assert not check.ok
    assert "it gained 0" in check.detail


def test_a_leaked_string_is_found_anywhere_in_a_payload() -> None:
    """Not only in `output` — which is the whole reason this assertion scans the payload.

    The string here sits inside a tool argument, which is exactly the place a leak would hide
    while an output-only check reported a clean run.
    """
    observation = Observation(
        steps=(
            Step(
                index=0,
                kind=INVOCATION,
                payload={
                    "output": "Done.",
                    "tool_calls": [{"name": "search_tickets", "arguments": {"q": "dev-key-123"}}],
                },
            ),
        ),
        tickets_before=0,
        tickets_after=0,
    )

    check = evaluate(
        {"assertions": [{"type": "payloads_avoid", "value": "dev-key-123"}]}, observation
    )[0]

    assert not check.ok, "a key in a tool argument must not pass a no-leak assertion"


def test_an_unknown_assertion_raises_rather_than_passing() -> None:
    """The one failure mode an eval must never have: silently measuring nothing."""
    with pytest.raises(UnknownAssertionError, match="unknown assertion type"):
        evaluate({"assertions": [{"type": "vibes_are_good"}]}, _successful_search_run())


def test_a_decision_assertion_needs_a_decision() -> None:
    with pytest.raises(UnknownAssertionError, match="needs a case with a decision"):
        evaluate(
            {"assertions": [{"type": "tickets_delta_before_decision", "value": 0}]},
            _successful_search_run(),
        )


def test_decision_outcome_is_read_from_the_decision_step_only() -> None:
    observation = Observation(
        steps=(
            Step(index=0, kind=INVOCATION, payload={"output": "proposed", "tool_calls": []}),
            Step(index=1, kind=DECISION, payload={"outcome": "declined", "detail": "no"}),
        ),
        tickets_before=1,
        tickets_after=1,
    )

    passed = evaluate(
        {"assertions": [{"type": "decision_outcome_is", "value": "declined"}]}, observation
    )
    failed = evaluate(
        {"assertions": [{"type": "decision_outcome_is", "value": "executed"}]}, observation
    )

    assert passed[0].ok
    assert not failed[0].ok


# --- 3. the report ------------------------------------------------------------


def test_the_report_lists_a_failure_with_its_reason() -> None:
    result = CaseResult(
        case_id="broken_case",
        family="the suite can fail",
        why="because a suite that cannot fail measures nothing",
    )
    result.checks = evaluate(load_cases(BROKEN)[0], _successful_search_run())

    markdown = render_markdown([result], duration_s=1.0, passed=summarise([result]))

    assert "**FAIL**" in markdown
    assert "### `broken_case`" in markdown
    assert "because a suite that cannot fail measures nothing" in markdown
    assert "expected a create_ticket call" in markdown


def test_a_green_report_says_none_under_failures() -> None:
    """A Failures section that lists passing cases would train a reader to ignore it."""
    result = CaseResult(case_id="fine", family="out-of-scope request refused", why="a claim")
    result.checks = evaluate(
        {"assertions": [{"type": "tool_called", "name": "search_tickets"}]},
        _successful_search_run(),
    )

    markdown = render_markdown([result], duration_s=1.0, passed=True)

    assert "**All 1 case(s) passed**" in markdown
    assert "None." in markdown

    # Sliced to the Failures section alone. The first version of this asserted the id appeared
    # nowhere after the heading, which is false for the right reason: the coverage section that
    # follows lists every case, so the check failed on a report that was correct.
    failures_section = markdown.split("## Failures")[1].split("## ")[0]
    assert "fine" not in failures_section
    assert "None." in failures_section


# --- 4. the loader rejects what it should ------------------------------------


def _write(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_case_with_no_steps_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, {"cases": [{"id": "empty", "user_id": "u", "steps": []}]})

    with pytest.raises(ValueError, match="has no steps"):
        load_cases(path)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    one = {"id": "same", "user_id": "u", "steps": [{"invoke": "hi"}]}
    path = _write(tmp_path, {"cases": [one, dict(one)]})

    with pytest.raises(ValueError, match="duplicate case id"):
        load_cases(path)


def test_half_a_two_user_setup_is_rejected(tmp_path: Path) -> None:
    """The isolation case needs both halves; one alone would silently test nothing."""
    case = {
        "id": "half",
        "user_id": "u",
        "steps": [{"invoke": "hi"}],
        "second_user_id": "other",
    }
    path = _write(tmp_path, {"cases": [case]})

    with pytest.raises(ValueError, match="both or neither"):
        load_cases(path)


def test_a_step_with_an_unknown_kind_is_rejected(tmp_path: Path) -> None:
    """A step the runner does not perform must fail the load, not be skipped at run time.

    The failure mode this prevents is a case that looks like it does something and does not: a
    suite that silently measures less than it says is worse than no suite at all.
    """
    case = {"id": "unknown", "user_id": "u", "steps": [{"shrug": True}]}
    path = _write(tmp_path, {"cases": [case]})

    with pytest.raises(ValueError, match="not exactly one of"):
        load_cases(path)


def test_a_step_carrying_two_kinds_is_rejected(tmp_path: Path) -> None:
    """Two keys would be ambiguous, and the runner would do whichever it happened to check first."""
    case = {"id": "two", "user_id": "u", "steps": [{"invoke": "hi", "restart": True}]}
    path = _write(tmp_path, {"cases": [case]})

    with pytest.raises(ValueError, match="not exactly one of"):
        load_cases(path)


def test_a_restart_that_is_not_true_is_rejected(tmp_path: Path) -> None:
    case = {"id": "restart", "user_id": "u", "steps": [{"restart": "yes"}]}
    path = _write(tmp_path, {"cases": [case]})

    with pytest.raises(TypeError, match="a restart is"):
        load_cases(path)


def test_a_sleep_that_is_not_a_duration_is_rejected(tmp_path: Path) -> None:
    """`true` is an int in Python, so without the explicit bool check it would sleep a second
    and look like it worked."""
    case = {"id": "sleep", "user_id": "u", "steps": [{"sleep_s": True}]}
    path = _write(tmp_path, {"cases": [case]})

    with pytest.raises(TypeError, match="not a duration"):
        load_cases(path)


def test_an_override_the_eval_cannot_vary_is_rejected(tmp_path: Path) -> None:
    """A case that sets a setting nothing reads would appear to configure something and not.

    That is the same defect class as the deadline setting that was documented and never passed:
    invisible in a passing run, and found only by someone who needed it to work.
    """
    case = {
        "id": "override",
        "user_id": "u",
        "steps": [{"invoke": "hi"}],
        "stack": {"SOMETHING_ELSE": "1"},
    }
    path = _write(tmp_path, {"cases": [case]})

    with pytest.raises(ValueError, match="does not vary"):
        load_cases(path)


def test_an_override_value_that_is_not_a_string_is_rejected(tmp_path: Path) -> None:
    """Environment variables are strings; a number here would be passed through as one."""
    case = {
        "id": "override-type",
        "user_id": "u",
        "steps": [{"invoke": "hi"}],
        "stack": {"AGENT_APPROVAL_TTL_S": 1},
    }
    path = _write(tmp_path, {"cases": [case]})

    with pytest.raises(TypeError, match="not a string"):
        load_cases(path)


def test_the_cases_that_bring_up_their_own_stack_vary_only_what_the_eval_varies() -> None:
    """Checked over the real file, not only the synthetic ones above.

    A case naming a key the eval does not reset would be a case whose result depended on the order
    it ran in — the one thing per-case stacks exist to prevent. And the `assert declared` is the
    half that keeps this from passing vacuously if the override cases are ever removed.
    """
    declared = [case for case in load_cases(CASES) if case.get("stack")]

    assert declared, "no case declares overrides, so this guard is measuring nothing"
    for case in declared:
        assert set(case["stack"]) <= CASE_OVERRIDABLE_ENV, case["id"]
