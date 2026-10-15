"""The running system, started the way a person starts it.

An eval that imports the agent's internals and calls them directly measures the internals. This
module deliberately does not: it starts the mock and the agent as real servers on real ports, and
every assertion the suite makes travels over HTTP. What that buys is the ability to be wrong about
the *assembly* — a handler that was never wired, a route whose prefix drifted, a provider whose
credentials were never read — because those are the defects that survive a green unit suite.

Three details are decisions rather than conveniences.

**One mock and one agent for the whole run, with chaos toggled.** The failure cases need the
backend to be failing, and the mock reads its chaos flag per request rather than at import.
Toggling it is therefore a real change of backend behaviour, and it means the agent is not
restarted between cases — so a case cannot pass because a restart papered over state the previous
case left behind.

**A case that needs different process settings gets its own stack, and says so.** A turn budget
and an approval TTL are read once, when the application is built, so no per-request trick can vary
them — and a case that appeared to configure one while changing nothing would be worse than a case
that admitted it could not. Such a case declares its overrides in `cases.json`; the runner starts a
stack for it and shuts it down afterwards. Two of the three cases that do this exist to prove that
*durable* state survives configuration being rebuilt underneath it, which the shared stack cannot
show.

**The database is migrated, not created.** ``build_app`` deliberately does not call ``create_all``
(ADR-0004): Alembic owns the schema, so the eval runs the same ``alembic upgrade head`` a
developer does, against a throwaway file. A suite that built its tables from the models would
happily pass against a schema no migration produces.
"""

from __future__ import annotations

import importlib.util
import itertools
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import httpx
import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[1]
MOCK_DIR = REPO_ROOT / "mock-ticketing"
AGENT_DIR = REPO_ROOT / "services" / "agent"

#: The mock's own default key, taken from its source rather than invented.
API_KEY = "dev-key-123"

#: Environment the mock reads at import, with the values a well-behaved case wants.
_MOCK_ENV: dict[str, str] = {
    "API_KEY": API_KEY,
    "FLAKY": "0",
    "FLAKY_RATE": "0.3",
    "FLAKY_SEED": "42",
    "LATENCY_MS": "0",
}

#: A different module name per load, so two stacks in one process cannot share configuration.
_MODULE_COUNTER = itertools.count()

#: The settings a *case* may override, and therefore the ones that must not survive from one
#: stack to the next. They are process-level values rather than per-request ones — a turn budget
#: and an approval TTL are read once, when the application is built — so a case that needs a
#: different one needs a different stack, and a case that sets one must not be able to change the
#: behaviour of the case after it. Popping these before every stack is what makes the file's case
#: order irrelevant; without it, whether a case passed would depend on what ran before it.
#:
#: A developer's own value for any of these is deliberately ignored by the suite — a rule stated
#: here so it is not discovered as a surprise.
CASE_OVERRIDABLE_ENV: frozenset[str] = frozenset(
    {"AGENT_REQUEST_DEADLINE_S", "AGENT_APPROVAL_TTL_S", "AGENT_MAX_STEPS"}
)

_READY_TIMEOUT_S = 60.0
_SHUTDOWN_TIMEOUT_S = 15.0

#: How long a single well-behaved case may take. Generous because the agent spawns the MCP
#: server as a subprocess on its first turn, and that process has to import a package.
_REQUEST_TIMEOUT_S = 60.0


class StackError(RuntimeError):
    """The stack could not be brought up. Distinct from a case failing."""


def _import_file(path: Path, module_name: str) -> ModuleType:
    """Import a module from an explicit file path under a chosen name."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise StackError(f"cannot load a module from {path}")

    module = importlib.util.module_from_spec(spec)
    # Registered before executing, so a module that imports itself resolves to this instance
    # rather than loading a second time.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_mock_module() -> ModuleType:
    """Load ``mock-ticketing/app.py`` as a fresh module under a unique name."""
    if not (MOCK_DIR / "app.py").is_file():
        raise StackError(f"the mock is not at {MOCK_DIR}")

    if str(MOCK_DIR) not in sys.path:
        sys.path.insert(0, str(MOCK_DIR))

    # ``app.py`` does ``from seed import SEED``, so seed.py has to resolve under that plain name.
    if "seed" not in sys.modules:
        sys.modules["seed"] = _import_file(MOCK_DIR / "seed.py", "seed")

    return _import_file(MOCK_DIR / "app.py", f"_eval_mock_{next(_MODULE_COUNTER)}")


def _free_port() -> int:
    """A port nothing is listening on, chosen by the OS."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _await_ready(base_url: str, path: str) -> None:
    """Block until a server answers, or fail with why it never did."""
    deadline = time.monotonic() + _READY_TIMEOUT_S
    last_error: BaseException | None = None

    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}{path}", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.1)

    raise StackError(f"nothing answered at {base_url}{path}: {last_error!r}")


@dataclass(frozen=True)
class Mock:
    """The ticketing mock, running, plus the module serving it."""

    base_url: str
    module: ModuleType

    @property
    def ticket_count(self) -> int:
        """How many tickets the mock holds, read from its own store.

        Not asked over HTTP on purpose. The question is "did this case change the backend", and
        the store *is* the backend; asking a route would mean asking the thing under test to
        report on itself, through the same middleware the failure cases switch on.
        """
        return len(cast(Any, self.module).DB)

    def set_chaos(self, *, enabled: bool, rate: float = 1.0) -> None:
        """Fail requests, or stop failing them. Effective on the next call.

        The middleware reads both values per request, so this is a change of backend behaviour
        rather than a mock of one.
        """
        cast(Any, self.module).FLAKY = enabled
        cast(Any, self.module).FLAKY_RATE = rate


@dataclass
class Stack:
    """The mock and the agent, both running, both reachable over HTTP."""

    mock: Mock
    agent_base_url: str
    work_dir: Path
    #: The database the agent is pointed at. Kept so a restart can be seen to resume the same one
    #: rather than quietly starting from an empty file, which would make the restart case pass for
    #: the wrong reason.
    database_url: str
    #: The agent's environment, kept so a restart brings it back exactly as it went down. Without
    #: it, a restart would silently drop to the defaults and a case that set a tighter budget
    #: would come back with the looser one. It includes the run's provider, so a restart cannot
    #: quietly fall back to `scripted` half-way through a live run.
    env: dict[str, str]
    _servers: list[tuple[uvicorn.Server, threading.Thread]]
    #: The agent alone, so it can be stopped and rebuilt without touching the mock.
    _agent: tuple[uvicorn.Server, threading.Thread] | None = None
    #: The application object, for the one thing only it can do: dispose the engine it opened.
    _agent_app: Any = None

    async def invoke(
        self,
        *,
        prompt: str,
        user_id: str,
        session_id: str | None = None,
        timeout_s: float = _REQUEST_TIMEOUT_S,
    ) -> dict[str, Any]:
        """One turn, through the same endpoint the platform calls."""
        body: dict[str, Any] = {"prompt": prompt, "user_id": user_id}
        if session_id is not None:
            body["session_id"] = session_id

        async with httpx.AsyncClient(base_url=self.agent_base_url, timeout=timeout_s) as client:
            response = await client.post("/invocations", json=body)
            return _checked_json(response, "POST /invocations")

    async def decide(
        self,
        *,
        approval_id: str,
        user_id: str,
        approved: bool,
        tolerate: Collection[int] = (),
        timeout_s: float = _REQUEST_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Approve or decline a proposal, through the route the chat uses.

        ``tolerate`` names status codes that are an *expected* answer rather than a fault, and a
        tolerated response comes back as a payload carrying ``status`` instead of raising. One case
        needs it: an expired proposal is refused with a 409, and that refusal is the thing being
        asserted. Any status not named here — a 500 in particular — still raises, so declaring an
        expectation cannot hide a real fault behind it.
        """
        async with httpx.AsyncClient(base_url=self.agent_base_url, timeout=timeout_s) as client:
            response = await client.post(
                f"/invocations/approvals/{approval_id}/decide",
                json={"user_id": user_id, "approved": approved},
            )

        if response.status_code in tolerate:
            return {"status": response.status_code, "error": _error_detail(response)}
        return _checked_json(response, "POST /invocations/approvals/{id}/decide")

    def restart_agent(self) -> None:
        """Tear the agent down and build it again, against the same database and the same mock.

        This is a redeploy, as far as a test in one process can be one. Everything the durability
        requirement actually depends on is rebuilt: the ASGI application and its settings, the
        database engine, and the lifespan — which stops the MCP client and its ticketing server
        subprocess, then starts both again. What is *not* reproduced is the process id, and that
        is the honest limit of this method: an approval that survives here survived a rebuilt
        application reading a durable file. The store itself under a genuinely new process is
        covered by the repository tests, which need no server.

        The old engine is disposed rather than left to garbage collection. Two engines open on one
        SQLite file is how a restart test produces a "database is locked" failure and gets blamed
        on the code under test.
        """
        if self._agent is None:
            raise StackError("the agent is not running, so there is nothing to restart")

        previous_server, previous_thread = self._agent
        previous_server.should_exit = True
        previous_thread.join(timeout=_SHUTDOWN_TIMEOUT_S)

        engine = getattr(getattr(self._agent_app, "state", None), "engine", None)
        if engine is not None:
            engine.dispose()

        app = _build_agent_app(self.env)
        port = _free_port()
        server, thread = _serve(app, port, f"eval-agent-{port}")

        self.agent_base_url = f"http://127.0.0.1:{port}"
        self._agent = (server, thread)
        self._agent_app = app
        self._servers = [entry for entry in self._servers if entry[1] is not previous_thread] + [
            (server, thread)
        ]
        # Waited on for the same reason as at startup: a request that arrives before the lifespan
        # finished would see no tool catalog and fail as "the service is still starting up".
        _await_ready(self.agent_base_url, "/ping")

    def shutdown(self) -> None:
        """Stop both servers and join their threads."""
        for server, _ in self._servers:
            server.should_exit = True
        for _, thread in self._servers:
            thread.join(timeout=_SHUTDOWN_TIMEOUT_S)


def _checked_json(response: httpx.Response, what: str) -> dict[str, Any]:
    """Decode a response, turning anything unexpected into a readable failure.

    A case failing on "expected completed, got 500" is far less useful than being told the
    endpoint answered 500 and what it said. This is where that sentence is written.
    """
    if response.status_code >= 400:
        raise StackError(f"{what} answered {response.status_code}: {response.text[:400]}")

    payload = response.json()
    if not isinstance(payload, dict):
        raise StackError(f"{what} returned {type(payload).__name__}, expected an object")
    return payload


def _error_detail(response: httpx.Response) -> str:
    """The sentence a refusal carried, or the raw body when it was not the expected shape.

    The contract's error envelope is ``{"error": {"detail": ...}}``. Decoded defensively and
    without a model: this is only reached for a status a case declared it expected, and failing to
    parse it should not turn an asserted refusal into an unexplained error.
    """
    try:
        body = response.json()
    except ValueError:
        return response.text[:400]

    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("detail"), str):
            return str(error["detail"])
    return response.text[:400]


def _migrate(database_url: str) -> None:
    """Apply the migrations to the throwaway database.

    Run as a subprocess with ``cwd`` set to the agent, for the reason ``devtasks migrate`` does
    the same: ``alembic.ini``'s script location is relative, so the migration only resolves
    against the same working directory the service will use.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=AGENT_DIR,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise StackError(
            "alembic upgrade head failed, so the eval database has no schema. "
            f"stdout: {result.stdout.strip()[-500:]} stderr: {result.stderr.strip()[-500:]}"
        )


def _serve(app: Any, port: int, name: str) -> tuple[uvicorn.Server, threading.Thread]:
    """Run an ASGI app under uvicorn in a background thread."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name=name, daemon=True)
    thread.start()
    return server, thread


def _case_overrides(overrides: Mapping[str, str] | None) -> dict[str, str]:
    """A case's environment overrides, with any earlier case's cleared out first.

    The settings a case may set are read once, when the application is built, so a case that needs
    a different one needs a different stack. Because the environment is process-wide, a value left
    behind by one case would silently apply to the next — and whether that case passed would
    depend on its position in the file, which is the one thing a suite must never do.

    The keys are removed rather than restored to remembered values on purpose: a developer's own
    value for one of them is ignored by the suite. That is a rule, and it is stated here so it is
    not discovered as a surprise.
    """
    for key in CASE_OVERRIDABLE_ENV:
        os.environ.pop(key, None)
    return dict(overrides or {})


def _build_agent_app(env: Mapping[str, str]) -> Any:
    """Build the agent's application under a specific environment.

    The settings cache is cleared first, and that is the whole reason this is a function rather
    than two lines in `start_stack`. ``build_app`` reads the environment once and caches the
    result, so without the clear a second stack in the same process would silently reuse the
    *first* stack's settings: a tighter turn budget set for one case would apply to the next, and
    the suite would report a case as configured that it never actually configured. That failure is
    invisible in a passing run, which is what makes it worth a function and a paragraph.
    """
    os.environ.update(env)

    from agent_service.config import clear_settings_cache
    from agent_service.factory import build_app

    clear_settings_cache()
    return build_app()


def start_stack(
    *,
    overrides: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> Stack:
    """Bring up the mock and the agent, and wait for both to answer.

    The order matters: the agent's lifespan connects to the ticketing MCP server, which talks to
    the mock, and it verifies the tool catalog during that handshake. Starting the agent first
    would make the handshake the thing that fails, and it would fail as a startup error rather
    than as the configuration mistake it is.

    ``overrides`` are settings for this stack's agent. They are not merged into a shared stack
    because they cannot be: both of the settings a case has needed so far are read when the
    application is built. A case that needs one therefore gets a stack of its own, which the
    runner starts and stops around it.

    ``extra_env`` is the *run's* environment rather than a case's — in practice the model provider
    and, for a live one, its credential. It is separate from ``overrides`` because the two are
    deliberately not the same thing. A case's overrides are limited to the settings a case may
    vary, for the reason recorded at that constant: a value one case could set would leak into the
    case after it, and whether a case passed would depend on its position in the file. The provider
    must instead reach *every* stack in the run, including the ones a case brings up for itself —
    otherwise a run against a live model would quietly serve its process-configuration cases with
    `scripted`, and the report would claim a model for cases that never touched one.
    """
    if not (AGENT_DIR / "pyproject.toml").is_file():
        raise StackError(f"the agent service is not at {AGENT_DIR}")

    work_dir = Path(tempfile.mkdtemp(prefix="agentcore-eval-"))
    case_env = _case_overrides(overrides)

    for key, value in _MOCK_ENV.items():
        os.environ[key] = value
    mock_module = _load_mock_module()
    mock_port = _free_port()
    mock_url = f"http://127.0.0.1:{mock_port}"
    mock_server, mock_thread = _serve(
        cast(Any, mock_module).app, mock_port, f"eval-mock-{mock_port}"
    )
    _await_ready(mock_url, "/health")

    database_url = f"sqlite+pysqlite:///{work_dir / 'eval.sqlite3'}"
    _migrate(database_url)

    # The agent's environment: its own settings, the values the MCP subprocess inherits for the
    # mock's address, whatever this case asked to vary, and the run's provider last.
    #
    # The ledger path is the run's own file. The ticketing server's idempotency ledger is a SQLite
    # file whose default path is relative — so by default it outlives the mock, which is in memory
    # and rebuilt on every run. A ledger that remembers a key the mock has never heard of makes the
    # *second* run of the suite behave differently from the first: the write is answered from the
    # record, no ticket is created, and a case asserting one ticket fails for a reason that has
    # nothing to do with the agent. Pointing it at the run's own directory is what makes the suite
    # reproducible, which is the one property an eval cannot do without.
    agent_env: dict[str, str] = {
        "DATABASE_URL": database_url,
        "TRACE_DIR": str(work_dir / "traces"),
        "MCP_TRANSPORT": "stdio",
        "TICKETING_BASE_URL": mock_url,
        "TICKETING_API_KEY": API_KEY,
        "TICKETING_LEDGER_PATH": str(work_dir / "ticketing-idempotency.sqlite3"),
        **case_env,
        # Last, so the run's provider and its credential cannot be displaced by a case override or
        # by a value the developer happens to have exported in their shell.
        **dict(extra_env or {}),
    }

    if str(AGENT_DIR / "src") not in sys.path:
        sys.path.insert(0, str(AGENT_DIR / "src"))

    app = _build_agent_app(agent_env)

    agent_port = _free_port()
    agent_url = f"http://127.0.0.1:{agent_port}"
    agent_server, agent_thread = _serve(app, agent_port, f"eval-agent-{agent_port}")
    # `/ping` is the contract's health route, so waiting on it also proves the contract is mounted.
    _await_ready(agent_url, "/ping")

    return Stack(
        mock=Mock(base_url=mock_url, module=mock_module),
        agent_base_url=agent_url,
        work_dir=work_dir,
        database_url=database_url,
        env=dict(agent_env),
        _servers=[(mock_server, mock_thread), (agent_server, agent_thread)],
        _agent=(agent_server, agent_thread),
        _agent_app=app,
    )
