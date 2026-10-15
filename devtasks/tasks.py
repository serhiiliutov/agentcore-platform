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
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict
from urllib.error import URLError
from urllib.parse import urlparse, urlunsplit
from urllib.request import Request, urlopen

from dotenv import dotenv_values

from devtasks.runner import (
    ENV_FILE,
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

if TYPE_CHECKING:
    # Only for the annotation on the capture helpers. Importing the driver at module scope would
    # make every devtask depend on a websocket client being importable, which is one command's
    # business rather than the runner's.
    from devtasks.chrome import Chrome

AGENT_DIR = REPO_ROOT / "services" / "agent"
MCP_DIR = REPO_ROOT / "services" / "mcp-ticketing"
MOCK_DIR = REPO_ROOT / "mock-ticketing"
EVAL_DIR = REPO_ROOT / "eval"
DATA_DIR = REPO_ROOT / "data"
MEDIA_DIR = REPO_ROOT / "docs" / "media"

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
    # Chainlit creates these two wherever it is started: `.chainlit/translations/` is a copy of
    # the dependency's own UI strings, and `.files/` is its upload directory. Both are re-created
    # on every run and both are gitignored, so scanning them proves nothing about this repository
    # while contributing twenty findings of somebody else's text — enough on its own to fail the
    # gate on a working tree that is entirely clean.
    r"|(^|[\\/])(\.chainlit|\.files)[\\/]"
    # `.env` itself, and this one is policy rather than noise. It is gitignored, and it is the
    # documented place for a credential — so scanning it makes the gate fail at exactly the moment
    # a developer configures a live provider, and the advice it then prints ("remove it from the
    # tree and rotate it") is wrong twice over: the value is not in the tree, and git will not put
    # it there. `.env.example` is tracked and stays covered, which is the part that matters —
    # the template must never grow a real secret. The residual gap is a deliberate `git add -f
    # .env`, and a commit that does that is visible in review as exactly what it is.
    r"|(^|[\\/])\.env$"
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
    are public: the starter kit's documented `dev-key-123`; **two** canary strings,
    one in tests/unit/test_providers.py asserting a key never appears in a log or a
    response, and one in tests/unit/test_loop_control.py asserting that a provider's
    own refusal — which quotes the key it rejected — never reaches the response
    either; and one false positive where the matched text is the *name* of an
    environment variable rather than a value.

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
    """Run the agent service against the locally started mock. Blocking; it serves the API only.

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


#: Where the chat front end listens. Deliberately not Chainlit's default 8000, and that is a
#: collision rather than a preference: the local stack runs the mock ticketing API on 8000, so a
#: chat UI on the same port would refuse to start at exactly the moment somebody tried to use it.
CHAT_PORT = 8001

#: The chat app. Its own uv project, and it can never join this workspace: Chainlit requires
#: ``mcp<2`` while this workspace's MCP server requires ``mcp>=2``, so a single shared resolution
#: has to downgrade one of them and the MCP server stops importing. See services/chat/pyproject.toml.
CHAT_DIR = REPO_ROOT / "services" / "chat"

#: The chat front end's own settings, and the only variables read out of `.env` for it.
#:
#: An allow-list rather than the whole file, and that is a boundary rather than tidiness: `.env`
#: also holds the ticketing API key and the agent's database URL, and this front end has no use for
#: either. Handing a subprocess every secret in a file because the file happens to be nearby is how
#: a credential ends up somewhere it was never meant to be — the same separation the three-service
#: split exists to draw.
CHAT_ENV_KEYS: tuple[str, ...] = (
    "AGENT_BASE_URL",
    "CHAT_USER_ID",
    "CHAT_USERS",
    "CHAINLIT_AUTH_SECRET",
    "CHAT_TIMEOUT_S",
)

#: The name compose gives the agent service — resolvable inside the compose network and nowhere
#: else. Worth recognising by name because `.env` is written *for* compose and sets
#: ``AGENT_BASE_URL=http://agent:8080``, so a chat started on the host from that file reported the
#: agent as unreachable while the stack was up and healthy.
COMPOSE_AGENT_HOST = "agent"
#: Where that agent is published on the host, for when the compose name is translated.
HOST_AGENT_PORT = 8080


def chat(extra: Sequence[str] = ()) -> int:
    """Run the Chainlit chat front end against a running agent. Blocking.

    `uv run --project services/chat` rather than this environment's interpreter, because the app
    lives in its own environment (see ``CHAT_DIR``). That command creates and locks the environment
    on first use, so there is nothing to install by hand — and, more importantly, it cannot disturb
    the agent's environment, which is the whole point of the split.

    The agent has to be running (`devtasks serve`, with `devtasks mock` behind it). The app talks to
    it over HTTP and says so plainly on the page when it cannot reach it, rather than failing in a
    way that looks like a front-end bug.

    ``cwd=CHAT_DIR``, and it is not cosmetic: Chainlit resolves ``.chainlit/`` and ``.files/``
    against the **current working directory** — ``APP_ROOT = os.getenv("CHAINLIT_APP_ROOT",
    os.getcwd())`` in its ``config.py`` — not against the app file. Running from the repository root
    therefore reads the root's config and writes a ``.files/`` directory there, which is how a stray
    top-level ``.chainlit/`` appeared. Pinning the cwd keeps both inside the service that owns them.

    **Login needs two variables, and Chainlit's own failure for the second one is a traceback.**
    Setting ``CHAT_USERS`` makes the app register a password callback; Chainlit then refuses to
    start without ``CHAINLIT_AUTH_SECRET`` — the key it signs the session cookie with — and it makes
    that check *before importing the app*, so nothing in ``app.py`` can intercept it. The guard
    below turns those six lines of traceback into one sentence naming both variables, which is the
    same shape as every other configuration failure in this file.
    """
    environment = _chat_environment()
    if environment.get("CHAT_USERS") and not environment.get("CHAINLIT_AUTH_SECRET"):
        raise TaskError(
            "CHAT_USERS is set, so the chat would show a login screen — and Chainlit will not "
            "start one without a signing secret for its session cookie. Generate one and put it "
            "where CHAT_USERS is, either in .env or in the environment:\n"
            "  uv run --project services/chat chainlit create-secret   # prints the line to add\n"
            "It has to be a real secret wherever the chat is shared: it is what stops a session "
            "cookie being forged, and every instance must use the same value or a restart logs "
            "everyone out. Unset CHAT_USERS to run anonymously instead."
        )
    if environment.get("CHAINLIT_AUTH_SECRET") and not environment.get("CHAT_USERS"):
        # The half-configured case, and the silent one: a secret with no accounts reads like "login
        # is set up" and then offers no login screen at all. Not an error — the anonymous chat is a
        # legitimate way to run — but it must never be a surprise, which is exactly what it was.
        print(
            "note: CHAINLIT_AUTH_SECRET is set but CHAT_USERS is not, so there will be NO login "
            "screen — the chat runs as the single anonymous identity above. Add "
            "`CHAT_USERS=name:password` to .env to turn login on.",
            flush=True,
        )
    return run(
        [
            *resolve_tool("uv"),
            "run",
            "--project",
            str(CHAT_DIR),
            "chainlit",
            "run",
            str(CHAT_DIR / "app.py"),
            "--port",
            str(CHAT_PORT),
            *extra,
        ],
        cwd=CHAT_DIR,
        env=environment,
    )


def _chat_environment() -> dict[str, str]:
    """The environment for the chat: this shell, then `.env`, then the host translation.

    Precedence is worth stating because it is the opposite of the obvious one — a variable exported
    in this shell wins over `.env`. A value typed for one run is a decision about that run, while
    `.env` is the file's standing configuration, so the specific beats the general and the file can
    be left alone while somebody points the chat at a different agent for a minute.

    Reading `.env` at all is the reason this function exists. This repository's convention is that
    configuration lives in `.env` — ``cp .env.example .env`` is the first line of both README and
    HOW-TO-TEST — and compose honours that for the services. The chat, though, runs on the *host*,
    started by this task rather than by Docker, so before this step a `CHAT_USERS` or a
    `CHAINLIT_AUTH_SECRET` written into `.env` did nothing whatsoever: nothing read the file, and
    the chat silently started anonymously with no login screen and no complaint. Which is exactly
    how it was found — by being asked why a secret that had been added to `.env` had no effect.
    """
    environment = dict(os.environ)
    for key, value in _chat_env_from_dotenv().items():
        environment.setdefault(key, value)
    base_url = environment.get("AGENT_BASE_URL")
    if base_url:
        environment["AGENT_BASE_URL"] = _host_reachable(base_url)
    return environment


def _chat_env_from_dotenv() -> dict[str, str]:
    """The chat's own variables out of the repository `.env`, and nothing else.

    Read with ``python-dotenv`` rather than by hand so quoting, comments and ``${VAR}``
    interpolation behave exactly as they do for compose, which reads the same file. Only the keys in
    ``CHAT_ENV_KEYS`` are taken — see that constant for why there is a list rather than a copy.
    """
    if not ENV_FILE.exists():
        return {}
    return {
        key: value
        for key, value in dotenv_values(ENV_FILE).items()
        if key in CHAT_ENV_KEYS and isinstance(value, str) and value
    }


def _host_reachable(base_url: str) -> str:
    """Rewrite compose's service name to loopback, for a run outside the compose network.

    `.env` is written for compose, where ``agent`` is a name Docker's DNS resolves and
    ``localhost`` would be the wrong container. Off that network the reverse holds, so a chat
    started from this file reported the agent as unreachable while the stack was up and healthy —
    and told the reader to start a stack that was already running.

    Only the service name is translated, and only the host: a URL naming a real host, a remote
    agent, or the same service on another port comes back untouched, because guessing beyond the
    one case that cannot possibly work would quietly ignore a deliberate setting.
    """
    parsed = urlparse(base_url)
    if parsed.hostname != COMPOSE_AGENT_HOST:
        return base_url
    port = parsed.port or HOST_AGENT_PORT
    return urlunsplit((parsed.scheme or "http", f"127.0.0.1:{port}", parsed.path, "", ""))


#: Where the agent's API is, and where the front end is. Both are checked before a capture: the
#: chat renders perfectly well with no API behind it, and photographing that would produce a frame
#: labelled "a read" that shows a connection error.
AGENT_URL = "http://127.0.0.1:8080"
CHAT_URL = f"http://127.0.0.1:{CHAT_PORT}"
READ_PROMPT = "find tickets about the printer"
WRITE_PROMPT = "please raise a ticket: the printer on the third floor is jammed again"
#: The chat's own label on the button that runs a proposed write, used to click it.
#:
#: Not used to wait for it to disappear, which is what this comment claimed until it was measured:
#: the chat leaves the approval card on screen with its buttons after a decision, so that wait never
#: succeeds. The frame after the click waits for the decision's outcome instead — see
#: `_capture_happy_path`.
APPROVE_BUTTON = "Approve and run"


def screenshots(extra: Sequence[str] = ()) -> int:
    """Capture one run, start to finish, as PNGs in `docs/media/`. Mutates the file tree.

    The assignment asks for screenshots or a recording of a read, a proposed write, the approval
    and a failure being handled. A screenshot nobody can reproduce is worth little, so this drives
    a real browser over the Chrome DevTools Protocol against the running **chat** — the only front
    end there is, since the agent's own page was removed — and writes what it sees. See
    `devtasks/chrome.py` for the driver and why it is not Playwright.

    Three things have to be up, and it says which one is missing rather than timing out later:

        devtasks up             # the three services
        devtasks chat           # the front end at http://localhost:8001, in its own terminal

    Two modes, because they need two different stacks:

      * **default** — the read, the proposed write and the approval, against a healthy stack. Four
        frames: `01-loaded`, `02-read`, `03-proposed-write`, `04-approved-write`.
      * **`--failure`** — the backend failing and the agent saying so, against a stack whose mock
        is *actually* failing:

            devtasks down && FLAKY=1 FLAKY_RATE=1.0 devtasks up   # PowerShell: $env:FLAKY='1'; ...

        One frame: `05-failure-handled`.

    The failure mode is a separate run rather than an extra step in the first one, and that is not
    a convenience. With `FLAKY_RATE=1.0` every backend call fails, so the write the third frame
    proposes could never be executed — the approval in the fourth step would fail and the run
    would stop half-way. Screenshotting an approval against a backend that cannot approve anything
    would be a picture of the harness, not of the product.

    The task does not restart the mock itself to create the failure: it refuses to draw a failure
    the stack did not really produce.

    **A second capture needs a clean ledger.** The scripted provider sends a fixed idempotency key
    for the ticket it creates — that is what makes the eval's replay assertions deterministic — so a
    run against a stack that has already executed that write is refused as a duplicate and the
    approval step fails. Reset first, which drops the MCP server's ledger along with the mock's
    data::

        devtasks down -v && devtasks up

    That is a property of the scripted provider, not of the capture: against a live model the key
    would differ on every run.

    The browser is imported here rather than at module scope so that `devtasks help`, or any other
    task, does not depend on a websocket library being importable — the driver is one command's
    business, not the runner's.
    """
    from devtasks import chrome as browser_driver

    _require_running_stack()

    with browser_driver.Chrome.launch() as page:
        _sign_in(page)
        outputs = _capture_failure(page) if "--failure" in extra else _capture_happy_path(page)

    for written in outputs:
        print(f"wrote {written.relative_to(REPO_ROOT)}")
    print(
        f"\n{len(outputs)} screenshot(s) in docs/media/. These are captures of a real run; "
        f"re-run this task after a UI change rather than editing them."
    )
    return 0


def _capture_happy_path(page: Chrome) -> list[Path]:
    """A read, a proposed write and the approval, ending in frames 01-04.

    Every wait is on something the *product* produced — the tool's own name, which the chat uses as
    the step's title, and its own label on the approval button — rather than on a delay or on a
    count of DOM nodes. A frame therefore cannot be taken before the thing it is named after
    exists, and none of these waits knows a CSS class the chat is free to change.
    """
    outputs: list[Path] = []

    _await_ready(page)
    outputs.append(page.screenshot(MEDIA_DIR / "01-loaded.png"))

    _send(page, READ_PROMPT)
    _await_text(page, "search_tickets", timeout=90, description="the search step to appear")
    outputs.append(page.screenshot(MEDIA_DIR / "02-read.png"))

    _send(page, WRITE_PROMPT)
    _await_text(page, "create_ticket", timeout=90, description="the write to be proposed")
    _await_text(page, APPROVE_BUTTON, timeout=60, description="the approval to be offered")
    outputs.append(page.screenshot(MEDIA_DIR / "03-proposed-write.png"))

    page.click_text(APPROVE_BUTTON)
    # Wait for the decision's *outcome*, not for the button to go. The chat answers a decision with
    # its own message — `**executed** — …` in `services/chat/app.py::_apply_decision` — and it
    # leaves the approval card on screen with its buttons, so "Approve and run" disappearing waits
    # for something the product never does. That is measured rather than assumed: the click had
    # reached the agent (a `200` on `POST /invocations/approvals/<id>/decide` in the chat's log) and
    # the capture still timed out on this wait for sixty seconds.
    #
    # The word is `executed` specifically, because this frame is titled "approved write". Any other
    # outcome — `declined`, `failed`, `expired` — means the approval did not produce a ticket, and a
    # capture that photographed that under this name would be showing the opposite of what it says.
    _await_text(page, "executed", timeout=60, description="the approved write to be reported")
    outputs.append(page.screenshot(MEDIA_DIR / "04-approved-write.png"))
    return outputs


def _capture_failure(page: Chrome) -> list[Path]:
    """One read against a failing backend, ending in frame 05.

    The assertion is the failure *on screen* — the chat's own "FAILED" marker, which it puts at the
    top of a step whose call had an error outcome — rather than a particular message, so the frame
    proves the path was taken without pinning the wording of an error the MCP server is free to
    improve.
    """
    _await_ready(page)
    _send(page, READ_PROMPT)
    _await_text(page, "FAILED", timeout=180, description="the backend failure to surface")
    return [page.screenshot(MEDIA_DIR / "05-failure-handled.png")]


def _await_ready(page: Chrome) -> None:
    """Wait until the chat has booted and is usable, not merely mounted.

    Two conditions, and the second is the whole point: `#chat-input` appears as soon as React
    mounts, which is *before* the app has read `GET /api/providers` and sent the message that says
    what it is connected to. Waiting on the composer alone therefore photographs a page that has a
    header, a theme toggle and nothing else — which is the frame this produced, and it was caught by
    opening the image rather than by trusting the wait. "Loaded" has to mean the conversation is
    there.

    `Connected to` is the app's own opening line (`services/chat/app.py::on_chat_start`), so this
    stays a statement about the product: if the chat stops saying that, the capture should fail
    loudly rather than quietly photograph an empty page.
    """
    page.wait_for(
        "document.querySelector('#chat-input') !== null",
        timeout=45,
        description="the composer to render",
    )
    _await_text(page, "Connected to", timeout=45, description="the chat to open its conversation")


def _has_text(value: str) -> str:
    """A JavaScript expression that is true when any element on the page contains `value`.

    Text rather than a selector throughout the capture, and that is chosen rather than lazy: the
    strings being waited for are the *product's* own — the tool's name, which the chat uses as its
    step title, and the chat's label on the approval button — so a frame cannot be taken before the
    thing it is named after exists. A selector would instead encode this repository's guess at
    Chainlit's generated class names, which is a guess that breaks on a dependency upgrade and
    fails as "timed out" rather than as "the markup changed".
    """
    return f"(document.body.innerText || '').indexOf({json.dumps(value)}) !== -1"


def _await_text(
    page: Chrome,
    value: str,
    *,
    timeout: float,
    description: str,
) -> None:
    """Wait for `value` to appear anywhere on the page."""
    page.wait_for(_has_text(value), timeout=timeout, description=description)


def _send(page: Chrome, text: str) -> None:
    """Type a message into the chat's composer, submit it, and verify it was really sent.

    `type_keys` and `press_enter` rather than assigning a value, and that is not a detail: the
    composer is a controlled React input whose send button stays disabled until React's own state
    holds the text, and both of the obvious shortcuts fail *silently* against it. See
    `devtasks/chrome.py::type_keys` for what was measured.

    **Every wait here exists because of a way this failed while building the capture.** The first
    version typed, pressed Enter, and waited for the composer to clear; the capture then waited
    ninety seconds for a tool step that never arrived, with no `POST /invocations` in the chat's own
    log — which is how the cause was pinned to the send rather than to the agent. The composer had
    cleared because Chainlit *replaced* the node while the app was still booting (it re-renders once
    `GET /api/providers` answers, and swaps the composer for a stop button during a turn), so the
    keystrokes had landed in a detached node and nothing was sent at all. A probe that waited for the
    composer to settle first sent the same message on the first try.

    So, in order: wait for a composer to exist, type, assert the composer *holds* the text (React
    renders an empty one if the typing was lost), press Enter, and then wait for the message to appear
    in the transcript — the only one of the three that means "it was sent". A verdict based on the
    composer emptying cannot tell a submission from a replaced node, which is exactly the mistake this
    replaces.

    One retry, and only into an empty composer: if the text is still there, Enter went nowhere and is
    pressed again; if the composer is empty the message was submitted and the transcript is simply
    slower than the wait, so it waits again instead. That distinction is what stops a retry from
    sending the same message twice.
    """
    page.wait_for(
        "document.querySelector('#chat-input') !== null",
        timeout=30,
        description="the composer to exist before typing",
    )
    _type_message(page, text)
    page.press_enter("#chat-input")

    try:
        _await_text(page, text, timeout=25, description="the message to reach the transcript")
    except TaskError:
        if _composer_holds(page, text):
            page.press_enter("#chat-input")
        _await_text(page, text, timeout=45, description="the message to reach the transcript")


def _type_message(page: Chrome, text: str) -> None:
    """Put `text` in the composer, and refuse to continue unless it is really there."""
    page.type_keys("#chat-input", text)
    page.wait_for(
        f"(document.querySelector('#chat-input') || {{}}).value === {json.dumps(text)}",
        timeout=15,
        description="the composer to hold the message, which is how a lost keystroke shows up",
    )


def _composer_holds(page: Chrome, text: str) -> bool:
    """Whether the composer still contains `text` — i.e. Enter did not submit anything."""
    return bool(page.evaluate(f"(document.querySelector('#chat-input') || {{}}).value === {json.dumps(text)}"))


def _require_running_stack() -> None:
    """Refuse to draw a run nobody is serving, naming which piece is missing.

    Two things have to be up and they fail in different ways: the agent, which is the API the chat
    calls, and the chat itself. Reported separately because "start the chat in another terminal"
    and "start the stack" are different instructions, and a capture that named the wrong one would
    send somebody to look at a process that is running perfectly well.
    """
    if not _is_answering(f"{AGENT_URL}/ping"):
        raise TaskError(
            f"nothing is answering at {AGENT_URL}/ping, so there is no run to capture. "
            f"Start the stack first: `devtasks up`."
        )
    if not _is_answering(f"{CHAT_URL}/"):
        raise TaskError(
            f"nothing is answering at {CHAT_URL}/, so there is no front end to capture. "
            f"Start it in another terminal: `devtasks chat`."
        )


def _is_answering(url: str) -> bool:
    """Whether a URL answers with something other than an error. Never raises."""
    try:
        with urlopen(Request(url), timeout=2) as response:
            # `int(...)` rather than a bare comparison: urllib is untyped, so the status arrives as
            # `Any` and mypy would have this function return it as-is.
            return int(response.status) < 400
    except (URLError, OSError):
        return False


def _sign_in(page: Chrome) -> None:
    """Open the chat, signing in when it is configured to ask for one.

    Both configurations are supported deliberately, because both are supported by the app: with
    `CHAT_USERS` set it shows a login screen, and without it the visitor is anonymous. The
    credentials come from the same allow-list `devtasks chat` starts the app with, so the capture
    cannot disagree with the running process about who may log in — and a capture that assumed one
    of the two would fail on the other with a timeout naming the wrong thing.

    Chainlit's own field is labelled "Email address"; this app takes the account *name* in it, which
    is documented in HOW-TO-TEST.md and is why nothing here looks like an email.
    """
    page.goto(f"{CHAT_URL}/")
    # Wait for *either* screen before deciding which one this is. Checking for the login form
    # immediately after `goto` found nothing, because Chainlit had not rendered yet — so the sign-in
    # was skipped, and the wait below then timed out on a composer that was behind a login screen.
    page.wait_for(
        "document.querySelector('#email') !== null "
        "|| document.querySelector('#chat-input') !== null",
        timeout=45,
        description="the chat to render either the login screen or the conversation",
    )
    if page.evaluate("document.querySelector('#email') === null"):
        return

    accounts = _accounts_from_environment()
    if not accounts:
        raise TaskError(
            "the chat is asking for a login, but CHAT_USERS is in neither .env nor the "
            "environment, so this task has no account to sign in with. Set CHAT_USERS (see "
            "HOW-TO-TEST.md, section 2.1) or start the chat without it."
        )
    name, password = accounts[0]
    page.type_keys("#email", name)
    page.type_keys("#password", password)
    page.click_text("Sign In")
    page.wait_for(
        "document.querySelector('#chat-input') !== null",
        timeout=45,
        description="the signed-in chat to render its composer",
    )


def _accounts_from_environment() -> list[tuple[str, str]]:
    """`name:password` pairs from the chat's own settings, in the order they were written.

    Deliberately the same source the app reads, rather than a second place to configure a capture
    account: a duplicate would be one more thing to keep in step, and it would drift in the
    direction that matters — the capture signing in as somebody who no longer exists.
    """
    raw = _chat_environment().get("CHAT_USERS", "")
    accounts: list[tuple[str, str]] = []
    for entry in raw.split(","):
        name, _, password = entry.partition(":")
        if name.strip() and password:
            accounts.append((name.strip(), password))
    return accounts


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
    """Run the eval suite. Read-only; exits non-zero when a case fails.

    The suite starts and stops its own stack on free ports, so nothing has to be running first —
    which is what makes it usable as a gate. `EVAL_PROVIDER` chooses the model it runs against;
    it defaults to `scripted`, which needs no credential, and `eval/provider.py` refuses at the
    start rather than half-way through if a named live provider cannot be built.
    """
    if not (EVAL_DIR / "runner.py").exists():
        # A guard rather than a formality: this path is documented in the README as the command
        # that produces the committed results, so a missing runner must say so instead of failing
        # as an unhelpful "no module named eval.runner".
        raise TaskError(
            "the eval runner is missing (eval/runner.py), so nothing has been checked."
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
    reachable from `up`, `test-integration` and the buildx command.
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
        # The chat front end, if it is running. Probed as well as /ping because the UI being down
        # while the agent is healthy is a different fault — and it is the one a reviewer hits
        # first, since the agent serves no page of its own any more (see `client_routes.py`).
        # Unreachable here is not a fault: the chat is a host process, started by `devtasks chat`,
        # and the stack is perfectly usable without it.
        ("chat UI", f"http://127.0.0.1:{CHAT_PORT}/", {}),
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
    """Print a machine-readable summary of the repo layout. Read-only.

    The two directory lists describe what this *repository* contains, not what this machine
    happens to hold. The distinction is not theoretical: the working tree that produced this
    project also holds the internal planning notes, untracked, so a report built from the
    directory listing named `docs/plan` — a directory no clone has. It read as a description
    of the published repository and was in fact a description of one machine's disk.
    """
    report = {
        "repo_root": str(REPO_ROOT),
        "services": _published_dirnames(REPO_ROOT / "services"),
        "docs": _published_dirnames(REPO_ROOT / "docs"),
        "env_file": (REPO_ROOT / ".env").exists(),
    }
    print(json.dumps(report, indent=2))
    return 0


def _published_dirnames(parent: Path) -> list[str]:
    """The immediate subdirectories of `parent` that are tracked in this repository.

    Tracked rather than merely present — see `json_report`. `git ls-files` lists every file
    under the directory, and only the first path segment of each is kept, so a nested file such
    as `docs/adr/0001-*.md` contributes `adr` once.

    Falls back to the directory listing when git cannot answer — an exported tarball, a
    checkout with no repository — so the command still describes something useful instead of
    reporting an empty tree. The names are filtered by `is_dir()` either way, so a file at the
    top of the directory (`docs/README.md`) is never reported as one of its subdirectories.
    """
    if not parent.exists():
        return []

    completed = subprocess.run(
        ["git", "ls-files", "--", parent.relative_to(REPO_ROOT).as_posix()],
        cwd=str(REPO_ROOT),
        env=build_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return sorted(p.name for p in parent.iterdir() if p.is_dir())

    # `git ls-files` reports paths relative to the *repository root*, not to the pathspec, so
    # the segment after `parent`'s own depth is the one wanted: for `docs/adr/0001-*.md` with
    # parent `docs`, that is `adr`. Taking the first segment instead produced `docs`, which is
    # not a subdirectory of `docs` — the report came back empty, and the test that pins the
    # published subset is what caught it.
    depth = len(parent.relative_to(REPO_ROOT).parts)
    names: set[str] = set()
    for line in completed.stdout.splitlines():
        parts = line.strip().split("/")
        if len(parts) > depth:
            names.add(parts[depth])
    return sorted(name for name in names if (parent / name).is_dir())
# ---------------------------------------------------------------------------
# Traces
# ---------------------------------------------------------------------------
#: Where a run's spans are on *this* machine, when `--dir` does not say otherwise.
#:
#: `data/dbview/traces`, because that is where the documented way of reading a trace on the host
#: puts them: `HOW-TO-TEST.md` section 6 starts the stack with `docker-compose.dbview.yml`, which
#: replaces the agent's `/data` volume with `./data/dbview/`. A default of `data/traces` would be
#: the *service's* default rather than the reader's — and it is a directory nothing has written to,
#: which reads as "no traces yet" on a machine that has plenty.
#:
#: `.env` is deliberately not consulted. It sets `TRACE_DIR=/data/traces`, a path inside the agent
#: container: correct for compose, and a directory no host process can open. Reading it would make
#: this task fail with "no such directory" on a machine where the stack is running perfectly — the
#: same class of mistake as `AGENT_BASE_URL=http://agent:8080` for a host chat, which is why
#: `_host_reachable` exists.
TRACE_DIR_DEFAULT = REPO_ROOT / "data" / "dbview" / "traces"

#: How wide a rendered attribute value may get before it is elided. Wide enough for a ticket id or
#: an error sentence, narrow enough that a row stays readable in a terminal.
_TRACE_VALUE_CHARS = 44


def _trace_directory(override: str | None) -> Path:
    """Where to look for trace files: `--dir` when given, the service's default otherwise."""
    return Path(override) if override else TRACE_DIR_DEFAULT


def _read_spans(path: Path) -> list[dict[str, Any]]:
    """One trace file's spans, in the order they were recorded.

    A malformed line is reported with its number rather than skipped. The file is written
    all-at-once to a temporary path and moved into place (see `observability/tracer.py`), so a
    partial line means something is genuinely wrong — and a viewer that silently dropped it would
    show a run with a step missing and no indication that anything was lost.
    """
    spans: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            spans.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise TaskError(f"{path.name} line {number} is not JSON: {error}") from error
    return spans


def _moment(span: dict[str, Any], key: str) -> datetime | None:
    """A span's timestamp, or None when it is absent or unparseable.

    `None` rather than an exception: an `ended_at` of null is the documented shape of a span whose
    run died mid-turn, and a viewer that refused to print the interrupted run would be a viewer
    that fails on exactly the run worth looking at.
    """
    value = span.get(key)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _render_value(value: Any) -> str:
    """One attribute value, flattened to a single short line."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    flattened = " ".join(str(text).split())
    if len(flattened) > _TRACE_VALUE_CHARS:
        return flattened[: _TRACE_VALUE_CHARS - 1] + "…"
    return flattened


def _render_span(span: dict[str, Any], *, origin: datetime | None) -> str:
    """One span as a line: when, what, how long, and the attributes that explain it."""
    started = _moment(span, "started_at")
    offset = f"+{(started - origin).total_seconds():7.3f}s" if started and origin else " " * 9
    duration = span.get("duration_ms")
    duration_text = f"{duration}ms".rjust(7) if isinstance(duration, int) else "      -"

    attributes = span.get("attributes") or {}
    detail = "  ".join(f"{key}={_render_value(value)}" for key, value in attributes.items())
    line = f"  {offset}  {span.get('kind', '?')!s:<9} {span.get('name', '?')!s:<20}"
    line += f"{duration_text}"
    return f"{line}  {detail}" if detail else line


def _display_path(path: Path) -> str:
    """A path relative to the repository when it is inside it, the full path when it is not.

    `--dir` exists so that a trace directory *anywhere* can be read — a copy pulled out of the
    agent container, a checkout of somebody else's run. `relative_to` raises for precisely those,
    which is how this was found: the flag that makes the task useful on another machine's files
    failed on the first one tried.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _describe_file(path: Path, spans: list[dict[str, Any]]) -> str:
    """One trace file as a line of a listing: how long, how many steps, and whether it failed.

    Failures are lifted into the line rather than left in the attributes because the listing exists
    to answer "which run do I want to open", and "this one had a failed call" is the single most
    useful thing to know before opening anything.
    """
    started = _moment(spans[0], "started_at") if spans else None
    ended = _moment(spans[-1], "ended_at") if spans else None
    span = f"{(ended - started).total_seconds():.3f}s" if started and ended else "     ?"
    kinds = sorted({str(item.get("kind", "?")) for item in spans})
    failed = any((item.get("attributes") or {}).get("failed") for item in spans)
    marker = "FAILED" if failed else "ok    "
    return (
        f"  {path.stem[:12]}  {len(spans):>3} span(s)  {span:>8}  {marker}  "
        f"{', '.join(kinds) or '-'}"
    )


def traces(extra: Sequence[str] = ()) -> int:
    """Read a run's spans back. Read-only; needs no stack and no database.

        devtasks traces                  # the runs on disk, newest first
        devtasks traces 3f0c4e2a         # one run, step by step, by id prefix
        devtasks traces 3f0c4e2a --json  # the same run as the raw records

    The third of the three ways a run is readable, and the one that works when nothing else does:
    the `traces` table is the searchable summary and the client routes serve it over HTTP, while
    this reads the JSONL files the service writes beside them. So it answers "what did this run do"
    on a machine with no database, no stack and no network.

    It prints what the file holds and computes nothing it cannot see. In particular it does not
    report the provider, the model or the run's status: those live in the `traces` row, and a viewer
    that guessed them from the file would be inventing the fields a reader most wants to trust.

    Under plain compose the files are inside the agent container, so the documented way to read them
    on the host is the `dbview` override (`HOW-TO-TEST.md`, section 6) — which is where the default
    comes from. For anywhere else — a copy pulled out of a container, a colleague's run — pass
    `--dir`.
    """
    arguments = [item for item in extra if not item.startswith("--")]
    flags = {item for item in extra if item.startswith("--")}
    directory_flag = next(
        (item.split("=", 1)[1] for item in extra if item.startswith("--dir=")), None
    )
    if "--dir" in extra:
        position = extra.index("--dir")
        if position + 1 >= len(extra):
            raise TaskError("--dir needs a path, e.g. devtasks traces --dir data/traces")
        directory_flag = extra[position + 1]
        arguments = [item for item in arguments if item != directory_flag]

    directory = _trace_directory(directory_flag)
    if not directory.exists():
        raise TaskError(
            f"no trace directory at {directory}. It is created when the service records its first "
            f"run, so this usually means nothing has run yet — or that the service is running in "
            f"compose, where its traces are inside the container (see the help on this task)."
        )

    files = sorted(directory.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        print(f"no traces in {directory} yet.")
        return 0

    if not arguments:
        print(f"{len(files)} trace(s) in {directory}, newest first:\n")
        for path in files[:_trace_limit(flags, extra)]:
            try:
                print(_describe_file(path, _read_spans(path)))
            except TaskError as error:
                print(f"  {path.stem[:12]}  unreadable: {error}")
        print("\nOpen one with: devtasks traces <id>")
        return 0

    wanted = arguments[0]
    matches = [path for path in files if path.stem.startswith(wanted)]
    if not matches:
        raise TaskError(f"no trace id starts with {wanted!r} in {directory}")
    if len(matches) > 1:
        raise TaskError(
            f"{len(matches)} traces start with {wanted!r}; give more of the id: "
            + ", ".join(path.stem[:12] for path in matches[:5])
        )

    path = matches[0]
    spans = _read_spans(path)
    if "--json" in flags:
        print(json.dumps(spans, indent=2))
        return 0

    origin = _moment(spans[0], "started_at") if spans else None
    print(f"{path.stem}  ({len(spans)} span(s), from {_display_path(path)})")
    if not spans:
        print("  the file is empty — the run recorded no spans")
        return 0
    for span in spans:
        print(_render_span(span, origin=origin))
    return 0


def _trace_limit(flags: set[str], extra: Sequence[str]) -> int:
    """How many traces to list. `--limit N` or `--limit=N`; 20 by default."""
    default = 20
    if not any(item.startswith("--limit") for item in flags | set(extra)):
        return default
    raw = next(
        (item.split("=", 1)[1] for item in extra if item.startswith("--limit=")), None
    )
    if raw is None and "--limit" in extra:
        position = extra.index("--limit")
        raw = extra[position + 1] if position + 1 < len(extra) else None
    try:
        return max(1, int(raw or default))
    except ValueError as error:
        raise TaskError(f"--limit takes a number, got {raw!r}") from error
