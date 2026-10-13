"""devtasks — the cross-platform task runner for this repository.

Why this exists instead of a Makefile alone: development happens on Windows and
CI runs on Linux. A Makefile is the conventional entry point for reviewers and is
kept at the repository root, but every target in it is a one-line delegation to
this package, so the same commands work identically here:

    uv run python -m devtasks <command> [args...]

`uv run` is used rather than a bare interpreter so the pinned environment from
`uv.lock` is the one that executes the task, on every machine.

Run `uv run python -m devtasks help` for the command list, or
`uv run python -m devtasks doctor` when something is not working.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
