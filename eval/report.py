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

**The headline names the model.** "22 of 22 passed" is a claim about the orchestration when the
provider is `scripted`, and a claim about a model's judgement when it is not — and a result file
that did not say which would be cited as evidence for whichever the reader assumed. The provider
is therefore in the first line, in the JSON, and in the reproduction command.

The output is deliberately a plain table rather than a link into a dashboard. It goes into git, it
is diffed between commits, and it has to be readable in a terminal and on a code-review page.
"""

from __future__ import annotations

from eval.scoring import CaseResult

#: Longest detail kept in the table, so one wide failure cannot make the result file unreadable.
_MAX_DETAIL = 220


def summarise(results: list[CaseResult]) -> bool:
    """Whether any case failed. The one-line answer, for the headline and the exit code.

    Driven by `failed` rather than by `passed`, so a case that was deliberately skipped does not
    turn the run red. A skip is not a success, and the report says so in its own words; what it
    must not be is a failure, because then a live run could never be green for a reason nobody
    chose.
    """
    return not any(result.failed for result in results)


def _truncate(detail: str) -> str:
    """A single-line, bounded version of a failure message."""
    flattened = " ".join(detail.split())
    if len(flattened) <= _MAX_DETAIL:
        return flattened
    return flattened[: _MAX_DETAIL - 1] + "…"


def _counts(result: CaseResult) -> str:
    if result.skipped is not None or result.error is not None:
        return "—"
    passed = sum(1 for check in result.checks if check.ok)
    return f"{passed}/{len(result.checks)}"


def _provider_phrase(name: str, is_live: bool) -> str:
    """How the model is described in a sentence, without a credential anywhere near it."""
    if is_live:
        return f"on `{name}` — a live model"
    return f"on `{name}` — no model called, so this is a claim about the orchestration"


def _headline(
    results: list[CaseResult],
    *,
    duration_s: float,
    provider_name: str,
    provider_is_live: bool,
) -> str:
    """The one line, counting passes, failures and skips as three different things.

    A skip is named in the headline rather than folded away, because the number a reader takes from
    this line is "how much was measured". "22 of 22 passed" and "21 of 22 passed, 1 skipped" are
    different claims about the same service, and the second is the true one when a case was not
    run.
    """
    total = len(results)
    passed = sum(1 for result in results if result.passed)
    skipped = sum(1 for result in results if result.skipped is not None)

    if passed == total:
        verdict = f"**All {total} case(s) passed**"
    else:
        verdict = f"**{passed} of {total} case(s) passed**"
        if skipped:
            verdict += f", {skipped} skipped"
    return f"{verdict} in {duration_s:.1f}s {_provider_phrase(provider_name, provider_is_live)}."


def _table(results: list[CaseResult]) -> list[str]:
    lines = [
        "| Case | Family | Result | Checks |",
        "| --- | --- | --- | --- |",
    ]
    for result in results:
        if result.skipped is not None:
            verdict = "**skip**"
        else:
            verdict = "**FAIL**" if result.failed else "**pass**"
        lines.append(
            f"| `{result.case_id}` | {result.family or '—'} | {verdict} | {_counts(result)} |"
        )
    return lines


def _failures(results: list[CaseResult]) -> list[str]:
    """The detail behind every failure, which is the part anyone actually reads."""
    # `failed`, not `not passed`: a skipped case has no checks, and listing it here would produce an
    # entry with nothing under it — which reads as a failure whose evidence has gone missing.
    broken = [result for result in results if result.failed]
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


def _skips(results: list[CaseResult]) -> list[str]:
    """The cases that were not run, with the reason each declared for itself.

    A section of its own rather than a dash in a table cell. The difference between "this passed"
    and "this was never asked" is the difference between a suite that covers something and one that
    looks like it does, and that is worth four lines of markdown.
    """
    skipped = [result for result in results if result.skipped is not None]
    if not skipped:
        return []
    lines = ["## Skipped", ""]
    for result in skipped:
        lines.append(f"- `{result.case_id}` — {result.skipped}")
    lines.append("")
    return lines


def _coverage(results: list[CaseResult]) -> list[str]:
    """What was actually asserted, so a green run is legible rather than merely reassuring."""
    lines = ["## What was measured", ""]
    for result in results:
        if result.skipped is not None:
            lines.append(f"- `{result.case_id}` — (skipped)")
            continue
        if not result.checks:
            lines.append(f"- `{result.case_id}` — (no checks)")
            continue
        types = ", ".join(f"`{check.assertion.get('type', '?')}`" for check in result.checks)
        lines.append(f"- `{result.case_id}` — {types}")
    lines.append("")
    return lines


def _reproduce(provider_name: str, provider_is_live: bool) -> list[str]:
    """How to get this file again, which differs between the two kinds of run.

    A deterministic run is one command with no prerequisites. A live run needs the provider named
    and a credential present, and saying so is what stops the committed result being mistaken for
    something CI reproduces — it does not, and deliberately: the gate is the deterministic suite.
    """
    lines = ["## Reproducing this run", "", "```sh"]
    if provider_is_live:
        lines.append(f"EVAL_PROVIDER={provider_name} make eval")
        lines.extend(
            [
                "```",
                "",
                "The suite starts the mock and the agent on free ports, drives the agent's real HTTP",
                f"contract, and scores what came back. `EVAL_PROVIDER={provider_name}` makes the agent",
                "call a live model, whose credential is read from `.env` or the shell — so this run",
                "needs a key and a network. It is **not** the CI gate: the model is not deterministic,",
                "so a case that fails here may have failed because the model chose differently rather",
                "than because anything regressed. The committed gate is the `scripted` run.",
            ]
        )
    else:
        lines.append("make eval")
        lines.extend(
            [
                "```",
                "",
                "The suite starts the mock and the agent on free ports, drives the agent's real HTTP",
                "contract, and scores what came back. It uses the `scripted` model provider, so the same",
                "commit produces the same result — no API key and no network are required.",
            ]
        )
    return lines


def render_markdown(
    results: list[CaseResult],
    *,
    duration_s: float,
    passed: bool,
    provider_name: str = "scripted",
    provider_is_live: bool = False,
) -> str:
    """The whole result file.

    ``passed`` is taken as an argument rather than recomputed so that the headline and the process
    exit code come from one decision, made once, in the runner. Two places computing "did it pass"
    is two places that can disagree, and the one that disagrees with the exit code is believed.

    The provider defaults to the deterministic one rather than being required, because the default
    is what the suite actually does when nothing is configured: a caller that forgets to pass it
    produces a report that is true about a `scripted` run, which is the run it gets.
    """
    lines = [
        "# Eval results",
        "",
        _headline(
            results,
            duration_s=duration_s,
            provider_name=provider_name,
            provider_is_live=provider_is_live,
        ),
        "",
        *(_table(results)),
        "",
    ]
    if not passed:
        if provider_is_live:
            lines.extend(
                [
                    "A failing case exits non-zero, so this run would fail a pipeline that used it "
                    "as a gate.",
                    "It is a live-model run rather than the committed gate, so read each failure as a "
                    "finding before treating it as a regression.",
                    "",
                ]
            )
        else:
            lines.extend(["A failing case exits non-zero, so this run would fail CI.", ""])
    lines.extend(_failures(results))
    lines.extend(_skips(results))
    lines.extend(_coverage(results))
    lines.extend(_reproduce(provider_name, provider_is_live))
    return "\n".join(lines)
