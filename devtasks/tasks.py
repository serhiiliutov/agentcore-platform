"""The tasks themselves. One small function per command, no hidden state.

Every task is idempotent and safe to run in any order. Tasks that mutate
something (containers, the database, the file tree) say so in their docstring.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Sequence
from urllib.error import URLError
from urllib.request import Request, urlopen

from devtasks.runner import (
    REPO_ROOT,
    TaskError,
    python,
    require_docker,
    require_env_file,
    run,
    run_tool,
)

AGENT_DIR = REPO_ROOT / "services" / "agent"
MCP_DIR = REPO_ROOT / "services" / "mcp-ticketing"
MOCK_DIR = REPO_ROOT / "mock-ticketing"
EVAL_DIR = REPO_ROOT / "eval"
DATA_DIR = REPO_ROOT / "data"

COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
COMPOSE_CI_FILE = REPO_ROOT / "docker-compose.ci.yml"

#: Markers excluded from a unit-test run.
UNIT_MARKERS = "not integration and not live"
#: Marker used by tests that need the stack running.
INTEGRATION_MARKER = "integration"


# ---------------------------------------------------------------------------
# Quality gates
# ---------------------------------------------------------------------------
def lint(extra: Sequence[str] = ()) -> int:
    """Lint the repository with ruff. Read-only."""
    return run_tool("ruff", ["check", *extra])


def format_code(extra: Sequence[str] = ()) -> int:
    """Reformat with ruff and apply its safe fixes. Mutates files."""
    run_tool("ruff", ["format", *extra])
    return run_tool("ruff", ["check", "--fix", *extra])


def typecheck(extra: Sequence[str] = ()) -> int:
    """Type-check the services, tests and dev tooling with mypy. Read-only."""
    return run_tool("mypy", list(extra))


def test_unit(extra: Sequence[str] = ()) -> int:
    """Run tests that need no running stack and no model credential. Read-only."""
    return run_tool("pytest", ["-m", UNIT_MARKERS, *extra])


def test_integration(extra: Sequence[str] = ()) -> int:
    """Run tests that need the stack running (docker compose up). Read-only."""
    return run_tool("pytest", ["-m", INTEGRATION_MARKER, *extra])


def test(extra: Sequence[str] = ()) -> int:
    """Run the whole suite with coverage. Read-only."""
    return run_tool(
        "pytest",
        [
            "-m",
            f"{UNIT_MARKERS} or {INTEGRATION_MARKER}",
            "--cov",
            "--cov-report=term-missing",
            *extra,
        ],
    )


def secrets(extra: Sequence[str] = ()) -> int:
    """Scan the working tree for credentials. Read-only.

    Uses `detect-secrets` rather than the `gitleaks` binary so that the check also
    runs on a Windows checkout with no extra install. CI adds the gitleaks Action
    as a second, independent layer.
    """
    baseline = REPO_ROOT / ".secrets.baseline"
    args = ["scan", "--baseline", str(baseline)] if baseline.exists() else ["scan"]
    return run_tool("detect-secrets", [*args, *extra])


# ---------------------------------------------------------------------------
# Runtime: compose stack
# ---------------------------------------------------------------------------
def _compose(args: Sequence[str], *, ci: bool = False) -> int:
    require_docker()
    files = ["-f", str(COMPOSE_CI_FILE if ci else COMPOSE_FILE)]
    if ci and not COMPOSE_CI_FILE.exists():
        raise TaskError(f"{COMPOSE_CI_FILE.name} does not exist yet (added in Stage 14)")
    return run(["docker", "compose", *files, *args])


def up(extra: Sequence[str] = ()) -> int:
    """Build and start the whole stack in the background. Mutates containers."""
    require_env_file()
    return _compose(["up", "-d", "--build", *extra])


def down(extra: Sequence[str] = ()) -> int:
    """Stop the stack and remove its containers. Keeps the data volume."""
    return _compose(["down", *extra])


def ps(extra: Sequence[str] = ()) -> int:
    """Show service status, including healthcheck results. Read-only."""
    return _compose(["ps", *extra])


def logs(extra: Sequence[str] = ()) -> int:
    """Tail stack logs. Pass a service name to narrow it, e.g. `logs agent`."""
    return _compose(["logs", "-f", "--tail=100", *extra])


def reset_mock(extra: Sequence[str] = ()) -> int:
    """Restore the ticketing mock's seed data. Mutates the mock's state."""
    require_env_file()
    return _compose(["exec", "-T", "ticketing", "python", "-c", _RESET_SNIPPET, *extra])


_RESET_SNIPPET = (
    "import os,urllib.request;"
    "k=os.environ.get('API_KEY','dev-key-123');"
    "r=urllib.request.Request('http://127.0.0.1:8000/admin/reset',method='POST',"
    "headers={'X-API-Key':k});"
    "print(urllib.request.urlopen(r).read().decode())"
)


def mock(extra: Sequence[str] = ()) -> int:
    """Run the mock ticketing API locally, without Docker. Blocking.

    Honours FLAKY, FLAKY_RATE, FLAKY_SEED and LATENCY_MS from the environment, so
    fault-injection runs work the same way here as in a container.
    """
    return run(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--reload",
            *extra,
        ],
        cwd=MOCK_DIR,
    )


def mcp(extra: Sequence[str] = ()) -> int:
    """Run the MCP server locally over stdio. Blocking; used by tests."""
    return run([sys.executable, "-m", "ticketing_mcp", *extra])


# ---------------------------------------------------------------------------
# Runtime: database
# ---------------------------------------------------------------------------
def migrate(extra: Sequence[str] = ()) -> int:
    """Apply all Alembic migrations. Mutates the agent database."""
    return run([sys.executable, "-m", "alembic", "upgrade", "head", *extra], cwd=AGENT_DIR)


def downgrade(extra: Sequence[str] = ()) -> int:
    """Roll the agent database back to base. Mutates the agent database."""
    target = extra[0] if extra else "base"
    return run([sys.executable, "-m", "alembic", "downgrade", target], cwd=AGENT_DIR)


def revision(extra: Sequence[str] = ()) -> int:
    """Autogenerate a migration: `revision "add approval ttl"`."""
    if not extra:
        raise TaskError('a message is required, e.g. devtasks revision "add approvals"')
    message = " ".join(extra)
    return run(
        [sys.executable, "-m", "alembic", "revision", "--autogenerate", "-m", message],
        cwd=AGENT_DIR,
    )


# ---------------------------------------------------------------------------
# Evals
# ---------------------------------------------------------------------------
def evaluate(extra: Sequence[str] = ()) -> int:
    """Run the eval suite against a running stack. Read-only, exits non-zero on failure."""
    if not (EVAL_DIR / "runner.py").exists():
        raise TaskError(
            "the eval runner does not exist yet (eval/runner.py arrives in Stage 13). "
            "Nothing has been checked."
        )
    return python(["-m", "eval.runner", *extra])


# ---------------------------------------------------------------------------
# Composite and housekeeping
# ---------------------------------------------------------------------------
def ci(extra: Sequence[str] = ()) -> int:
    """Reproduce the CI pipeline locally: lint, type-check, unit tests, evals."""
    lint()
    typecheck()
    test_unit()
    evaluate(extra)
    print("\nCI pipeline passed.")
    return 0


def env(extra: Sequence[str] = ()) -> int:
    """Create `.env` from `.env.example` if it is missing. Mutates the file tree."""
    require_env_file()
    return 0


def clean(extra: Sequence[str] = ()) -> int:
    """Remove caches and local runtime data. Mutates the file tree.

    Never touches source. Pass `--deep` to also delete `.venv`.
    """
    deep = "--deep" in extra
    targets = [
        *(REPO_ROOT.glob("**/__pycache__")),
        REPO_ROOT / ".pytest_cache",
        REPO_ROOT / ".ruff_cache",
        REPO_ROOT / ".mypy_cache",
        REPO_ROOT / ".coverage",
        REPO_ROOT / "htmlcov",
        DATA_DIR,
    ]
    if deep:
        targets.append(REPO_ROOT / ".venv")

    removed = 0
    for target in targets:
        if not target.exists():
            continue
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
        removed += 1
        print(f"removed {target.relative_to(REPO_ROOT)}")
    print(f"clean: {removed} path(s) removed")
    return 0


def doctor(extra: Sequence[str] = ()) -> int:
    """Print environment diagnostics. Read-only, never fails on a missing tool."""
    checks: list[tuple[str, str]] = []
    checks.append(("python", sys.version.split()[0]))
    checks.append(("executable", sys.executable))
    checks.append(("platform", sys.platform))

    for tool in ("uv", "docker", "git", "ruff", "mypy", "pytest"):
        found = shutil.which(tool)
        checks.append((tool, found or "NOT FOUND"))

    checks.append(
        (".env", "present" if (REPO_ROOT / ".env").exists() else "missing (run: devtasks env)")
    )
    checks.append(("data/", "present" if DATA_DIR.exists() else "absent (created on first run)"))

    for provider, variable in (
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
        ("bedrock", "AWS_REGION"),
    ):
        configured = bool(os.environ.get(variable))
        checks.append((f"credential:{provider}", "configured" if configured else "not set"))

    width = max(len(name) for name, _ in checks)
    for name, value in checks:
        print(f"  {name.ljust(width)}  {value}")

    _print_mock_status()
    return 0


def _print_mock_status() -> None:
    """Report whether the stack's HTTP endpoints answer. Never raises."""
    endpoints: list[tuple[str, str, dict[str, str]]] = [
        ("ticketing mock", "http://127.0.0.1:8000/health", {}),
        ("agent /ping", "http://127.0.0.1:8080/ping", {}),
        ("web ui", "http://127.0.0.1:3000/", {}),
    ]
    for label, url, headers in endpoints:
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=2) as response:
                body = response.read(200).decode(errors="replace").strip()
                print(f"  {label.ljust(20)}  {response.status} {body[:80]}")
        except (URLError, OSError) as exc:
            print(f"  {label.ljust(20)}  unreachable ({type(exc).__name__})")


def json_report(extra: Sequence[str] = ()) -> int:
    """Print a machine-readable summary of the repo layout. Read-only."""
    report = {
        "repo_root": str(REPO_ROOT),
        "services": sorted(p.name for p in (REPO_ROOT / "services").iterdir() if p.is_dir())
        if (REPO_ROOT / "services").exists()
        else [],
        "docs": sorted(p.name for p in (REPO_ROOT / "docs").iterdir() if p.is_dir())
        if (REPO_ROOT / "docs").exists()
        else [],
        "env_file": (REPO_ROOT / ".env").exists(),
    }
    print(json.dumps(report, indent=2))
    return 0
