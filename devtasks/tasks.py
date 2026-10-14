"""The tasks themselves. One small function per command, no hidden state.

Every task is idempotent and safe to run in any order. Tasks that mutate
something (containers, the database, the file tree) say so in their docstring.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from typing import TypedDict
from urllib.error import URLError
from urllib.request import Request, urlopen

from devtasks.runner import (
    REPO_ROOT,
    TaskError,
    build_env,
    python,
    require_docker,
    require_env_file,
    resolve_tool,
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
#: Compose project for the CI stack, so it cannot collide with a developer's one.
CI_PROJECT_NAME = "agentcore-platform-ci"
#: The reviewed allow-list of secret-shaped values, read by :func:`secrets`.
SECRETS_BASELINE = ".secrets.baseline"
#: Directories the secret scan must not walk: local environments, caches, runtime data.
#:
#: `--exclude-files` takes a regular expression, and the leading separator is optional on
#: purpose. A top-level directory is walked as `.venv/Lib/...`, with nothing in front of it,
#: so an expression anchored on a separator matches none of it — the scan then reports
#: hundreds of findings inside third-party packages and looks like a gate doing its job.
#: That is not hypothetical: it is what the first version of this pattern did, and it took a
#: 193-second run and a read of the output to notice `.venv` was still in the results.
SECRETS_EXCLUDE = (
    # Local environments, caches and runtime data: third-party content, not ours.
    r"(^|[\\/])(\.venv|\.git|data|__pycache__|\.mypy_cache|\.ruff_cache|\.pytest_cache"
    r"|htmlcov|node_modules)[\\/]"
    # ...and the allow-list itself. Its `hashed_secret` values are hex strings and read as
    # secrets to the detector; scanning our own allow-list is circular, and without this the
    # gate fails on a clean tree. detect-secrets skips the baseline by itself only when it is
    # passed as `--baseline`, which this task does not do — the comparison is ours.
    rf"|(^|[\\/]){SECRETS_BASELINE.replace('.', r'\.')}$"
)

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
    """Scan the working tree for credentials, failing on anything unreviewed.

    Uses `detect-secrets` rather than the `gitleaks` binary so that the *same* check
    runs here and in CI, on a Windows checkout, with no extra install. CI runs this
    task rather than the gitleaks Action, which would need a licence secret on an
    organisation account and a third-party action otherwise; the trade is recorded
    in .github/workflows/ci.yml.

    The result is read back and judged here, and that is not belt-and-braces:
    `detect-secrets scan` exits 0 whether or not it found anything, because its job
    is to *write* a baseline, not to gate on one. Calling it as a gate would have
    produced a check that cannot fail — worse than no check at all, because it looks
    like one and gets cited as evidence.

    A finding is accepted only if its secret *hash* is already in
    `.secrets.baseline`, which is the record of the values we have read and decided
    are public: the starter kit's documented `dev-key-123`, the canary string that
    tests/unit/test_providers.py asserts never appears in a log or a response, and
    one false positive where the matched text is the *name* of an environment
    variable rather than a value.

    The comparison is by hash, not by path, and deliberately: the baseline was
    written on Windows, where detect-secrets records `devtasks\\tasks.py`, and a
    Linux runner records `devtasks/tasks.py`. A path-based check would therefore be
    green locally and red in CI for a reason that has nothing to do with a secret.
    The cost of hashing is that a *new* occurrence of an already-reviewed value
    passes — acceptable here, because every reviewed value is one this repository
    publishes on purpose.

    Two flags are always passed, and both were needed before this gate could be trusted
    to fail:

      * `--all-files`, because the default covers only files git already tracks. A
        credential in a new file — the usual way one arrives — would be invisible, and a
        scan that reads as thorough while covering only committed files is the same class
        of mistake as the one above.
      * `--exclude-files`, because `--all-files` also walks `.venv`, `.git` and the local
        `data/` directory, whose findings are third-party noise that would fail every run.
        See `SECRETS_EXCLUDE` for the anchoring trap in that pattern.
    """
    known = _reviewed_secret_hashes()
    argv = [
        *resolve_tool("detect-secrets"),
        "scan",
        # Without this the scan covers only files git already tracks — its own help says
        # "as compared to only scanning git tracked files" — so a credential in a new,
        # not-yet-committed file is invisible. That is the most likely way one arrives.
        "--all-files",
        "--exclude-files",
        SECRETS_EXCLUDE,
        *extra,
    ]
    print(f"\n$ {' '.join(argv)}", flush=True)

    completed = subprocess.run(
        argv,
        cwd=str(REPO_ROOT),
        env=build_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        print(completed.stderr, file=sys.stderr)
        raise TaskError(f"detect-secrets failed with exit code {completed.returncode}")

    report = _parse_secret_report(completed.stdout)
    findings: list[tuple[str, int, str]] = [
        (filename, finding["line_number"], finding["type"])
        for filename, entries in report.items()
        for finding in entries
        if finding["hashed_secret"] not in known
    ]

    if not findings:
        print(
            f"secrets: nothing new — {len(known)} reviewed value(s) in {SECRETS_BASELINE}, "
            f"{sum(len(entries) for entries in report.values())} finding(s), all recognised"
        )
        return 0

    print(f"\n{len(findings)} unreviewed finding(s):", file=sys.stderr)
    for filename, line_number, kind in sorted(findings):
        print(f"  {filename}:{line_number}  [{kind}]", file=sys.stderr)
    raise TaskError(
        f"the secret scan found {len(findings)} value(s) not in {SECRETS_BASELINE}. If one is a "
        f"real credential, remove it from the tree and rotate it. If it is genuinely not a "
        f"secret, regenerate the baseline with `detect-secrets scan > {SECRETS_BASELINE}` and say "
        f"in the commit message what you checked."
    )


def _reviewed_secret_hashes() -> set[str]:
    """The hashes of the values in `.secrets.baseline`. Empty when there is no baseline."""
    path = REPO_ROOT / SECRETS_BASELINE
    if not path.exists():
        return set()
    return {
        finding["hashed_secret"]
        for entries in _parse_secret_report(path.read_text(encoding="utf-8")).values()
        for finding in entries
    }


class _SecretFinding(TypedDict):
    """The fields of one detect-secrets finding that this task reads.

    Typed rather than `dict[str, object]`, because both comprehensions that consume
    the report index into it: with `object` values mypy cannot tell that
    `line_number` is an `int` or that `hashed_secret` is a `str`, and casting at each
    use would hide the same uncertainty from the reader instead of resolving it once
    here.
    """

    type: str
    filename: str
    hashed_secret: str
    line_number: int


def _parse_secret_report(document: str) -> dict[str, list[_SecretFinding]]:
    """Parse a detect-secrets report, with an error that says what to do."""
    try:
        parsed = json.loads(document)
    except json.JSONDecodeError as error:
        raise TaskError(f"could not read the detect-secrets report as JSON: {error}") from error
    results = parsed.get("results")
    if not isinstance(results, dict):
        raise TaskError("the detect-secrets report has no `results` object")
    return results


# ---------------------------------------------------------------------------
# Runtime: compose stack
# ---------------------------------------------------------------------------
def _compose(args: Sequence[str], *, ci: bool = False) -> int:
    """Run a compose command against the development stack, or the CI one.

    The CI stack is the development file *plus* an override, not a second file that
    restates the services: `docker-compose.ci.yml` holds only what CI changes, so
    there is one topology and one place its settings live. See that file for why
    `include` — the tidier-looking option — does not work here.

    `-p` gives the CI stack its own compose project. Without it both stacks would be
    project `agentcore-platform`, and `up-ci` would replace a developer's running
    containers rather than standing beside them.
    """
    require_docker()
    if ci:
        if not COMPOSE_CI_FILE.exists():
            raise TaskError(f"{COMPOSE_CI_FILE.name} is missing, so the CI stack cannot start")
        files = ["-f", str(COMPOSE_FILE), "-f", str(COMPOSE_CI_FILE)]
        project = ["-p", CI_PROJECT_NAME]
    else:
        files = ["-f", str(COMPOSE_FILE)]
        project = []
    return run(["docker", "compose", *files, *project, *args])


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


def up_ci(extra: Sequence[str] = ()) -> int:
    """Start the CI stack: the same three services with deterministic settings.

    This exists so that the stack the workflow runs is one a developer can run too.
    `docker-compose.ci.yml` was written for CI and nothing invoked it, which made it
    a file only CI could reach — and a topology nobody can reproduce locally is a
    topology nobody can debug. The pins live in that file; this is only the way in.

    `--wait` rather than a sleep, for the same reason the workflow uses it: it blocks
    until every service is healthy and exits non-zero if one never is, so a broken
    stack fails here rather than in the test that follows. It is also why this task
    builds: a CI stack that reused stale images would test the previous commit.
    """
    require_env_file()
    return _compose(["up", "-d", "--build", "--wait", *extra], ci=True)


def down_ci(extra: Sequence[str] = ()) -> int:
    """Stop the CI stack. Pass `-v` to drop its volumes as well."""
    return _compose(["down", *extra], ci=True)


def ps_ci(extra: Sequence[str] = ()) -> int:
    """Show the CI stack's status, including healthcheck results. Read-only."""
    return _compose(["ps", *extra], ci=True)


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


#: Values a run against the *locally* started mock needs, which ``.env`` cannot supply.
#:
#: ``.env`` is written for the compose stack: it points the MCP server at
#: ``http://ticketing:8000``, a hostname that resolves only inside the compose network, and it
#: selects the ``http`` MCP transport because compose runs the server as a container. On the host
#: the mock is on the loopback interface and the MCP server is a child process, so a run that
#: inherited the compose values could not reach anything — which is exactly how this was found:
#: the agent started, the MCP child exited immediately, and the service refused to start.
LOCAL_SERVE_ENV: dict[str, str] = {
    "MCP_TRANSPORT": "stdio",
    "TICKETING_BASE_URL": "http://127.0.0.1:8000",
    "TICKETING_API_KEY": "dev-key-123",
}


def _local_env() -> dict[str, str]:
    """The environment for a host run: everything set, plus local defaults where unset.

    ``setdefault`` rather than assignment, so a variable the developer exported explicitly still
    wins. A run that silently ignored an env var would be worse than one that failed, because it
    would look like it had honoured the setting.
    """
    environment = dict(os.environ)
    for key, value in LOCAL_SERVE_ENV.items():
        environment.setdefault(key, value)
    return environment


def serve(extra: Sequence[str] = ()) -> int:
    """Run the agent service against the locally started mock. Blocking; the page is at ``/``.

    Run with ``cwd=AGENT_DIR`` for exactly the reason ``migrate`` is: the default
    ``DATABASE_URL`` is a relative SQLite path, so the database Alembic created is the one the
    service opens only if both resolve it against the same directory.

    Starting the service from the repository root would instead open
    ``<repo>/data/agent.sqlite3`` — a second, empty file — and the failure would present as a
    missing table on the first request rather than as a path mistake, which is the kind of
    diagnosis that costs an afternoon. Pinning the working directory here means the two cannot
    disagree, instead of relying on a README paragraph to keep them in step.

    The local overrides are applied for the same class of reason — see ``LOCAL_SERVE_ENV``.
    """
    return run([sys.executable, "-m", "agent_service", *extra], cwd=AGENT_DIR, env=_local_env())


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
    """Autogenerate a migration.

    Arguments are forwarded to ``alembic revision --autogenerate`` verbatim, so both
    forms work::

        devtasks revision -m "add approval ttl"
        devtasks revision --rev-id 0002 -m "add retry ledger"
    """
    if not extra:
        raise TaskError('a message is required, e.g. devtasks revision -m "add approvals"')
    return run(
        [sys.executable, "-m", "alembic", "revision", "--autogenerate", *extra],
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
    """Reproduce the CI pipeline locally: lint, type-check, unit tests, evals.

    The secret scan is included because the workflow runs it, and a local `ci` that
    skipped a gate the pipeline enforces would let exactly the mistake it guards
    against reach the branch. Everything else the workflow does — building the
    images, starting the stack, the arm64 cross-build — needs Docker and is
    reachable from `up`, `test-integration` and the buildx command in
    docs/plan/stage-14.md.
    """
    lint()
    typecheck()
    test_unit()
    secrets()
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
        # The page is served by the agent itself (Stage 11), so it is the agent's port. Probed
        # as well as /ping because a page that fails while health answers is a different fault,
        # and the one a reviewer would hit first.
        ("agent web page", "http://127.0.0.1:8080/", {}),
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
