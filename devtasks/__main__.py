"""Command-line entry point: ``python -m devtasks <command> [args...]``.

Dispatch is deliberately manual rather than argparse subparsers. Every task
accepts a trailing list of extra arguments that it forwards to the underlying
tool (``devtasks logs agent``, ``devtasks pytest -k approval``), and
``argparse.REMAINDER`` interacts badly with that. A dict lookup plus ``sys.argv``
slicing is easier to read and impossible to get subtly wrong.

Exit codes are honest: 0 on success, 1 when a task fails, 130 on Ctrl-C.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence

from devtasks import tasks
from devtasks.runner import TaskError

TaskFn = Callable[[Sequence[str]], int]

#: name → (callable, one-line description, example usage)
COMMANDS: dict[str, tuple[TaskFn, str, str]] = {
    # --- orientation -------------------------------------------------------
    "help": (lambda _args: _help(), "show this list", "devtasks help"),
    "doctor": (tasks.doctor, "check tools, credentials and running services", "devtasks doctor"),
    "json": (tasks.json_report, "repo layout as JSON", "devtasks json"),
    # --- quality gates -----------------------------------------------------
    "lint": (tasks.lint, "ruff check", "devtasks lint"),
    "fmt": (tasks.format_code, "ruff format + safe fixes (mutates files)", "devtasks fmt"),
    "typecheck": (tasks.typecheck, "mypy", "devtasks typecheck"),
    "test-unit": (
        tasks.test_unit,
        "pytest, no stack and no credential needed",
        "devtasks test-unit",
    ),
    "test-integration": (
        tasks.test_integration,
        "pytest -m integration, needs the stack up",
        "devtasks test-integration",
    ),
    "test": (tasks.test, "whole suite with coverage", "devtasks test"),
    "secrets": (tasks.secrets, "detect-secrets scan of the working tree", "devtasks secrets"),
    "ci": (tasks.ci, "lint + typecheck + unit tests + evals", "devtasks ci"),
    # --- runtime -----------------------------------------------------------
    "up": (tasks.up, "build and start the stack (mutates containers)", "devtasks up"),
    "down": (tasks.down, "stop the stack", "devtasks down"),
    "ps": (tasks.ps, "service status and health", "devtasks ps"),
    "logs": (tasks.logs, "tail logs; pass a service name to narrow", "devtasks logs agent"),
    # The same three services with CI's deterministic settings, on their own compose
    # project, so that the stack the workflow uses is one a developer can run too.
    "up-ci": (tasks.up_ci, "start the CI stack and wait for health", "devtasks up-ci"),
    "down-ci": (tasks.down_ci, "stop the CI stack (add -v to drop volumes)", "devtasks down-ci -v"),
    "ps-ci": (tasks.ps_ci, "CI stack status, including healthchecks", "devtasks ps-ci"),
    "reset": (tasks.reset_mock, "restore the ticketing seed data", "devtasks reset"),
    "mock": (tasks.mock, "run the mock ticketing API locally (blocking)", "devtasks mock"),
    "mcp": (tasks.mcp, "run the MCP server locally over stdio (blocking)", "devtasks mcp"),
    # --- database ----------------------------------------------------------
    "migrate": (tasks.migrate, "alembic upgrade head", "devtasks migrate"),
    "serve": (tasks.serve, "run the agent service (web page at /)", "devtasks serve"),
    "downgrade": (tasks.downgrade, "alembic downgrade (default: base)", "devtasks downgrade base"),
    "revision": (tasks.revision, "autogenerate a migration", 'devtasks revision "add approvals"'),
    # --- evals and housekeeping -------------------------------------------
    "eval": (tasks.evaluate, "run the eval suite against the running stack", "devtasks eval"),
    "env": (tasks.env, "create .env from .env.example if missing", "devtasks env"),
    "clean": (
        tasks.clean,
        "remove caches and local data (--deep also .venv)",
        "devtasks clean --deep",
    ),
}

#: Accepted aliases → canonical command name.
ALIASES: dict[str, str] = {
    "format": "fmt",
    "check": "ci",
    "pytest": "test-unit",
    "tests": "test",
    "start": "up",
    "stop": "down",
    "--help": "help",
    "-h": "help",
    "list": "help",
}


def _help() -> int:
    """Print the command table. Always succeeds."""
    width = max(len(name) for name in COMMANDS)
    desc_width = max(len(desc) for _, desc, _ in COMMANDS.values())

    print("devtasks — repository task runner\n")
    print("  uv run python -m devtasks <command> [args...]\n")
    print("Commands:")
    for name in sorted(COMMANDS):
        _, description, usage = COMMANDS[name]
        print(f"  {name.ljust(width)}  {description.ljust(desc_width)}   e.g. {usage}")
    print("\nEvery command forwards trailing arguments to the underlying tool:")
    print("  devtasks test-unit -k approval -x")
    print("  devtasks lint --statistics")
    print("\nOn Windows, `make <target>` needs GNU Make installed; `python -m devtasks`")
    print("works with nothing but Python. The Makefile is a thin wrapper over this.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch a command. Returns the process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)

    if not args:
        _help()
        return 0

    requested = args[0]
    extra = args[1:]
    name = ALIASES.get(requested, requested)

    if name not in COMMANDS:
        print(f"unknown command: {requested}", file=sys.stderr)
        print("run `python -m devtasks help` to see the available commands", file=sys.stderr)
        return 2

    func, _, _ = COMMANDS[name]
    return int(func(extra))


if __name__ == "__main__":  # pragma: no cover - exercised by the CLI, not by unit tests
    try:
        raise SystemExit(main())
    except TaskError as error:
        print(f"\ntask failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130) from None
