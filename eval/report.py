"""Render a run as the markdown table a reviewer reads.

The committed result file is the answer to "what did the evals say", so it has to be readable by
someone who did not run them and cannot see the terminal. That drives two choices here.

**Every check is listed, not only the failures.** A table of "7 passed" tells a reviewer nothing
about what was measured; the same table with each assertion named tells them the suite is real.
Silence is not evidence.

**A failure carries its case's reason for existing.** The `why` field from `cases.json` is
reproduced next to the failure, so a red cell is never just a red cell: the reader is told both
what was expected and why anyone cared. A failure that has to be looked up in another file before
it means anything is a failure that gets ignored.

The output is deliberately a plain table rather than a link into a dashboard. It goes into git, it
is diffed between commits, and it has to be readable in a terminal and on a code-review page.
"""

from __future__ import annotations

from eval.scoring import CaseResult

#: Longest detail kept in the table, so one wide failure cannot make the result file unreadable.
_MAX_DETAIL = 220


def summarise(results: list[CaseResult]) -> bool:
    """Whether every case passed. The one-line answer, for the headline and the exit code."""
    return all(result.passed for result in results)


def _truncate(detail: str) -> str:
    """A single-line, bounded version of a failure message."""
    flattened = " ".join(detail.split())
    if len(flattened) <= _MAX_DETAIL:
        return flattened
    return flattened[: _MAX_DETAIL - 1] + "…"


def _counts(result: CaseResult) -> str:
    if result.error is not None:
        return "—"
    passed = sum(1 for check in result.checks if check.ok)
    return f"{passed}/{len(result.checks)}"


def _headline(results: list[CaseResult], *, duration_s: float) -> str:
    total = len(results)
    passed = sum(1 for result in results if result.passed)
    if passed == total:
        return f"**All {total} case(s) passed** in {duration_s:.1f}s."
    return f"**{passed} of {total} case(s) passed** in {duration_s:.1f}s."


def _table(results: list[CaseResult]) -> list[str]:
    lines = [
        "| Case | Family | Result | Checks |",
        "| --- | --- | --- | --- |",
    ]
    for result in results:
        verdict = "**pass**" if result.passed else "**FAIL**"
        lines.append(
            f"| `{result.case_id}` | {result.family or '—'} | {verdict} | {_counts(result)} |"
        )
    return lines


def _failures(results: list[CaseResult]) -> list[str]:
    """The detail behind every failure, which is the part anyone actually reads."""
    broken = [result for result in results if not result.passed]
    if not broken:
        return ["## Failures", "", "None.", ""]

    lines = ["## Failures", ""]
    for result in broken:
        lines.append(f"### `{result.case_id}`")
        lines.append("")
        if result.why:
            lines.append(f"*Why this case exists:* {result.why}")
            lines.append("")

        if result.error:
            # A case that could not run says so, rather than showing an empty list of checks and
            # leaving a reader to guess whether it passed.
            lines.append(f"- **the case did not complete:** {_truncate(result.error)}")
            lines.append("")
            continue

        for failure in result.failures:
            wanted = failure.assertion.get("type", "?")
            lines.append(f"- `{wanted}` — {_truncate(failure.detail)}")
        lines.append("")

    return lines


def _coverage(results: list[CaseResult]) -> list[str]:
    """What was actually asserted, so a green run is legible rather than merely reassuring."""
    lines = ["## What was measured", ""]
    for result in results:
        if not result.checks:
            lines.append(f"- `{result.case_id}` — (no checks)")
            continue
        types = ", ".join(f"`{check.assertion.get('type', '?')}`" for check in result.checks)
        lines.append(f"- `{result.case_id}` — {types}")
    lines.append("")
    return lines


def render_markdown(
    results: list[CaseResult],
    *,
    duration_s: float,
    passed: bool,
) -> str:
    """The whole result file.

    ``passed`` is taken as an argument rather than recomputed so that the headline and the process
    exit code come from one decision, made once, in the runner. Two places computing "did it pass"
    is two places that can disagree, and the one that disagrees with the exit code is believed.
    """
    lines = [
        "# Eval results",
        "",
        _headline(results, duration_s=duration_s),
        "",
        *(_table(results)),
        "",
    ]
    if not passed:
        lines.extend(["A failing case exits non-zero, so this run would fail CI.", ""])
    lines.extend(_failures(results))
    lines.extend(_coverage(results))
    lines.extend(
        [
            "## Reproducing this run",
            "",
            "```sh",
            "make eval",
            "```",
            "",
            "The suite starts the mock and the agent on free ports, drives the agent's real HTTP",
            "contract, and scores what came back. It uses the `scripted` model provider, so the same",
            "commit produces the same result — no API key and no network are required.",
        ]
    )
    return "\n".join(lines)
