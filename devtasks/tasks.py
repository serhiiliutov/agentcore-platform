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
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict
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


#: The page under capture, and the two turns the assignment asks to see.
PAGE_URL = "http://127.0.0.1:8080"
READ_PROMPT = "find tickets about the printer"
WRITE_PROMPT = "please raise a ticket: the printer on the third floor is jammed again"


def screenshots(extra: Sequence[str] = ()) -> int:
    """Capture one run, start to finish, as PNGs in `docs/media/`. Mutates the file tree.

    The assignment asks for screenshots or a recording of a read, a proposed write, the approval
    and a failure being handled. A screenshot nobody can reproduce is worth little, so this drives
    a real browser over the Chrome DevTools Protocol against the running service and writes what
    it sees — see `devtasks/chrome.py` for the driver and why it is not Playwright.

    Two modes, because they need two different stacks:

      * **default** — the read, the proposed write and the approval, against a healthy stack. Four
        frames: `01-loaded`, `02-read`, `03-proposed-write`, `04-approved-write`.
      * **`--failure`** — the backend failing and the agent saying so, against a stack whose mock
        is *actually* failing:

            devtasks down-ci
            FLAKY=1 FLAKY_RATE=1.0 devtasks up      # PowerShell: $env:FLAKY='1'; $env:FLAKY_RATE='1.0'

        One frame: `05-failure-handled`.

    The failure mode is a separate run rather than an extra step in the first one, and that is not
    a convenience. With `FLAKY_RATE=1.0` every backend call fails, so the write the third frame
    proposes could never be executed — the approval in the fourth step would fail and the run
    would stop halfway. Asserting an approval against a backend that cannot approve anything would
    be a screenshot of the harness, not of the product.

    The task does not restart the mock itself to create the failure: it refuses to draw a failure
    the stack did not really produce.

    The browser is imported here rather than at module scope so that `devtasks help`, or any other
    task, does not depend on a websocket library being importable — the driver is one command's
    business, not the runner's.
    """
    from devtasks import chrome as browser_driver

    agent_up = _agent_is_answering()
    if not agent_up:
        raise TaskError(
            f"nothing is answering at {PAGE_URL}/ping, so there is no run to capture. "
            f"Start the stack first: `devtasks up-ci` (or `devtasks up`)."
        )

    with browser_driver.Chrome.launch() as page:
        outputs = _capture_failure(page) if "--failure" in extra else _capture_happy_path(page)

    for written in outputs:
        print(f"wrote {written.relative_to(REPO_ROOT)}")
    print(
        f"\n{len(outputs)} screenshot(s) in docs/media/. These are captures of a real run; "
        f"re-run this task after a UI change rather than editing them."
    )
    return 0


def _capture_happy_path(page: Chrome) -> list[Path]:
    """A read, a proposed write and the approval, ending in frames 01-04."""
    outputs: list[Path] = []

    page.goto(f"{PAGE_URL}/")
    page.wait_for("document.querySelector('#ask') !== null", description="the page to render")
    # A capture run must start from a known state: a proposal left over from somebody's earlier
    # poking would appear in the first frame and make these screenshots describe another run.
    page.wait_for(
        "document.querySelectorAll('#pending .approval').length === 0",
        timeout=10,
        description="no proposal to be outstanding before the run starts",
    )
    outputs.append(page.screenshot(MEDIA_DIR / "01-loaded.png"))

    calls_before = _count(page, "#transcript .call")
    _submit(page, READ_PROMPT)
    _await_more(page, "#transcript .call", calls_before, "the read to call a tool", timeout=60)
    outputs.append(page.screenshot(MEDIA_DIR / "02-read.png"))

    _submit(page, WRITE_PROMPT)
    page.wait_for(
        "document.querySelector('#pending .approval') !== null",
        timeout=60,
        description="the write to be proposed rather than run",
    )
    outputs.append(page.screenshot(MEDIA_DIR / "03-proposed-write.png"))

    page.click("#pending .approval button.ok")
    page.wait_for(
        "document.querySelectorAll('#pending .approval').length === 0",
        timeout=60,
        description="the proposal to be decided",
    )
    page.wait_for(
        "Array.from(document.querySelectorAll('#transcript .turn .role'))"
        ".some(function (n) { return n.textContent.indexOf('write') === 0; })",
        timeout=30,
        description="the executed write to appear in the transcript",
    )
    outputs.append(page.screenshot(MEDIA_DIR / "04-approved-write.png"))
    return outputs


def _capture_failure(page: Chrome) -> list[Path]:
    """One read against a failing backend, ending in frame 05.

    The assertion is the error *on screen* — a red tool call in the transcript — rather than a
    particular message, so the frame proves the path was taken without pinning the wording of an
    error the MCP server is free to improve.
    """
    page.goto(f"{PAGE_URL}/")
    page.wait_for("document.querySelector('#ask') !== null", description="the page to render")
    _submit(page, READ_PROMPT)
    page.wait_for(
        "document.querySelector('#transcript .call.err') !== null",
        timeout=180,
        description="the backend failure to surface as an error tool call",
    )
    return [page.screenshot(MEDIA_DIR / "05-failure-handled.png")]


def _count(page: Chrome, selector: str) -> int:
    """How many elements match `selector` right now."""
    return int(page.evaluate(f"document.querySelectorAll({json.dumps(selector)}).length"))


def _await_more(
    page: Chrome,
    selector: str,
    previous: int,
    description: str,
    *,
    timeout: float,
) -> None:
    """Wait for a *new* match to appear, not merely for one to exist.

    Counting rather than testing for presence, because the transcript accumulates: after the first
    turn, "there is a tool call on screen" is already true, so a presence check would let the
    second capture fire before the second turn had produced anything and every later screenshot
    would show the previous turn's result.
    """
    page.wait_for(
        f"document.querySelectorAll({json.dumps(selector)}).length > {previous}",
        timeout=timeout,
        description=description,
    )


def _submit(page: Chrome, text: str) -> None:
    """Type a message, submit the form, and wait for it to appear in the transcript."""
    page.set_value("#message", text)
    page.click("#ask button[type=submit]")
    page.wait_for(
        "Array.from(document.querySelectorAll('#transcript .turn.user .said'))"
        f".some(function (n) {{ return n.textContent === {json.dumps(text)}; }})",
        timeout=15,
        description="the submitted message to appear in the transcript",
    )


def _agent_is_answering() -> bool:
    """Whether the agent's health endpoint answers. Never raises."""
    try:
        request = Request(f"{PAGE_URL}/ping")
        with urlopen(request, timeout=2) as response:
            # `int(...)` rather than `== 200` alone: urllib is untyped, so the comparison is
            # `Any` and mypy would have the function return it as-is.
            return int(response.status) == 200
    except (URLError, OSError):
        return False


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
