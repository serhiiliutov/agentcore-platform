"""Tests for the task runner's command dispatcher.

The task runner is Stage 0's main deliverable and the documented entry point for
every other command, so its dispatch table is worth testing: a typo in a command
name or an alias pointing at a command that no longer exists would otherwise be
found by whoever tries to use it.
"""

from __future__ import annotations

import json

import pytest

from devtasks.__main__ import ALIASES, COMMANDS, main


def test_no_arguments_prints_help_and_succeeds() -> None:
    """`python -m devtasks` on its own should orient the user, not fail."""
    assert main([]) == 0


def test_help_lists_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    """Every registered command must be discoverable from the help output."""
    assert main(["help"]) == 0

    printed = capsys.readouterr().out
    for name in COMMANDS:
        assert name in printed, f"{name} is registered but missing from `devtasks help`"


def test_help_shows_an_example_for_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    """A command without a usage example is a command nobody can guess."""
    main(["help"])
    printed = capsys.readouterr().out
    for name, (_, _, usage) in COMMANDS.items():
        assert usage in printed, f"{name} has no example shown in the help output"


def test_unknown_command_exits_two_and_explains(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown command is a usage error, and the message must say what to do."""
    exit_code = main(["definitely-not-a-command"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "unknown command" in captured.err
    assert "devtasks help" in captured.err


@pytest.mark.parametrize("alias", sorted(ALIASES))
def test_every_alias_points_at_a_registered_command(alias: str) -> None:
    """An alias that resolves to nothing is a broken entry point."""
    assert ALIASES[alias] in COMMANDS, f"alias {alias!r} points at an unregistered command"


def test_every_command_is_documented() -> None:
    """Each entry needs a callable, a one-line description and an example."""
    for name, (func, description, usage) in COMMANDS.items():
        assert callable(func), f"{name} has no callable"
        assert description.strip(), f"{name} has no description"
        assert description == description.strip(), f"{name}'s description has stray whitespace"
        assert usage.startswith("devtasks "), f"{name}'s example does not show a command invocation"


def test_help_aliases_are_accepted() -> None:
    """`--help`, `-h` and `list` are the forms people actually type."""
    for alias in ("--help", "-h", "list"):
        assert main([alias]) == 0


def test_json_report_emits_parseable_json(capsys: pytest.CaptureFixture[str]) -> None:
    """The JSON report is machine-read; it must always parse.

    The `docs` list is the *published* subset — the directories a reader of this repository
    finds. Asserting the working tree instead is how this was found: the machine that wrote
    the change also held the internal notes, untracked, so `"plan" in report["docs"]` passed
    here and failed in a clone of the very same commit. A test that describes one machine's
    disk rather than the repository is the defect, not the clone.
    """
    assert main(["json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert set(report) == {"repo_root", "services", "docs", "env_file"}
    assert "agent" in report["services"]
    assert "mcp-ticketing" in report["services"]
    assert {"adr", "media", "starter-kit"} <= set(report["docs"])
    assert not {"plan", "architecture", "runbooks"} & set(report["docs"])
