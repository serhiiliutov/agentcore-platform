"""Process execution helpers shared by every dev task.

Two things this module exists to solve:

1. **Tool resolution that works on a Windows checkout and in Linux CI.** Tools
   are looked up next to the running interpreter first (the uv-managed virtual
   environment), then on `PATH`, and finally as `python -m <tool>`. No task
   hardcodes `.exe`, a shell, or a path separator.
2. **Honest failures.** Every task returns a process exit code and prints the
   command before running it, so a failing CI log tells you exactly which command
   failed without guessing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Repository root — the directory that contains this package.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent

#: Environment the repo expects to exist; created from `.env.example` by `env`.
ENV_FILE: Path = REPO_ROOT / ".env"
ENV_EXAMPLE: Path = REPO_ROOT / ".env.example"


class TaskError(RuntimeError):
    """A dev task could not run. The message is written to be actionable."""


@dataclass(frozen=True)
class Command:
    """A command to run, already resolved to an executable."""

    argv: Sequence[str]
    cwd: Path = REPO_ROOT

    def display(self) -> str:
        return " ".join(self.argv)


def venv_bin_dir() -> Path:
    """Directory holding the console scripts of the environment we are running in."""
    return Path(sys.executable).parent


def resolve_tool(name: str) -> list[str]:
    """Resolve a console script to an argv prefix that will actually run.

    Order matters: the virtual environment we were launched from wins over
    anything on `PATH`, which is what stops a developer's globally installed
    ruff from being used instead of the pinned one.
    """
    candidates = (
        venv_bin_dir() / name,
        venv_bin_dir() / f"{name}.exe",
        venv_bin_dir() / f"{name}.cmd",
    )
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate)]

    on_path = shutil.which(name)
    if on_path:
        return [on_path]

    # Last resort: most tools are importable as a module.
    return [sys.executable, "-m", name]


def build_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for a child process, with the repo root importable.

    `PYTHONPATH` gains the two service source trees so that tasks which import
    service code (mypy, pytest, the eval runner) behave the same way whether
    they run inside the uv environment or in a bare interpreter.
    """
    env = dict(os.environ)
    extra_paths = [
        str(REPO_ROOT / "services" / "agent" / "src"),
        str(REPO_ROOT / "services" / "mcp-ticketing" / "src"),
        str(REPO_ROOT),
    ]
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join([*extra_paths, *([existing] if existing else [])])
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if extra:
        env.update(extra)
    return env


def run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    quiet: bool = False,
) -> int:
    """Run a command, streaming its output. Returns the exit code.

    Raises `TaskError` when `check` is set and the command fails — a failing task
    must never be mistaken for a passing one.
    """
    command = Command(argv=list(argv), cwd=cwd or REPO_ROOT)
    if not quiet:
        print(f"\n$ {command.display()}", flush=True)

    completed = subprocess.run(
        list(command.argv),
        cwd=str(command.cwd),
        env=build_env(env),
        check=False,
    )
    if check and completed.returncode != 0:
        raise TaskError(
            f"command failed with exit code {completed.returncode}: {command.display()}"
        )
    return completed.returncode


def run_tool(
    tool: str,
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    quiet: bool = False,
) -> int:
    """Resolve a tool by name and run it with `args`."""
    return run(resolve_tool(tool) + list(args), cwd=cwd, env=env, check=check, quiet=quiet)


def python(args: Sequence[str], *, check: bool = True, quiet: bool = False) -> int:
    """Run the current interpreter. Used for module entrypoints we own."""
    return run([sys.executable, *args], check=check, quiet=quiet)


def require_env_file() -> None:
    """Ensure `.env` exists, creating it from `.env.example` when it does not.

    Compose reads `.env`; the file is gitignored, so a fresh clone has none. This
    makes `make up` work on a clean checkout without a manual copy step.
    """
    if ENV_FILE.exists():
        return
    if not ENV_EXAMPLE.exists():
        raise TaskError(f"neither {ENV_FILE.name} nor {ENV_EXAMPLE.name} exists")
    shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
    print(f"created {ENV_FILE.name} from {ENV_EXAMPLE.name}")


def require_docker() -> None:
    """Fail early, with a clear message, when Docker is unavailable."""
    if shutil.which("docker") is None:
        raise TaskError(
            "docker was not found on PATH. Install Docker Desktop, or run the "
            "services directly (see HOW-TO-TEST.md)."
        )
