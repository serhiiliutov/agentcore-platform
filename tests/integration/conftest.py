"""A real mock, listening on a real port, for the integration suite.

The contract suite loads the mock's app object and drives it through `TestClient`, which is
in-process and does not open a socket. That is the right tool for describing the mock's
behaviour, and the wrong one for testing a *client*: `TestClient` satisfies an ASGI app
directly, so it would never exercise the things this suite is about — connection handling,
HTTP timeouts, or the fact that a retry is a second request on the wire.

So this fixture runs the same app under `uvicorn` on a free port and hands back its URL. The
app object is also returned, because some assertions are about the mock's own state rather
than about what a route said: reading the store directly cannot be confused by the chaos
middleware that the test is deliberately switching on.
"""

from __future__ import annotations

import importlib.util
import itertools
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import httpx
import pytest
import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[2]
MOCK_DIR = REPO_ROOT / "mock-ticketing"

#: The mock's own default key, taken from its source rather than invented.
API_KEY = "dev-key-123"

#: Environment the mock reads at *import* time, with the values a well-behaved test wants.
_DEFAULT_ENV: dict[str, str] = {
    "API_KEY": API_KEY,
    "FLAKY": "0",
    "FLAKY_RATE": "0.3",
    "FLAKY_SEED": "",
    "LATENCY_MS": "0",
}

#: Every load gets its own module name, so two tests can load the same file with different
#: environment variables without one seeing the other's configuration.
_MODULE_COUNTER = itertools.count()

_READY_TIMEOUT_S = 15.0
_SHUTDOWN_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class ServedMock:
    """A mock that is up and answering, plus the module running it."""

    base_url: str
    module: ModuleType

    @property
    def ticket_count(self) -> int:
        """How many tickets the mock is holding, read from its own store.

        Deliberately not asked over HTTP. The assertion a caller wants here is "did this write
        change the backend", and the store *is* the backend; asking a route instead would mean
        asking the thing under test to report on itself, through the same middleware the test
        is exercising.
        """
        return len(cast(Any, self.module).DB)

    def set_flaky(self, *, enabled: bool) -> None:
        """Turn the mock's chaos on or off, the way an operator would by restarting the API.

        The middleware reads this flag per request rather than once at import, so the change
        takes effect on the very next call — which is what makes "and then the API recovered"
        a deterministic step in a test instead of a wait for a probability to come out right.
        """
        cast(Any, self.module).FLAKY = enabled


def _import_file(path: Path, module_name: str) -> ModuleType:
    """Import a module from an explicit file path under a chosen name."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load a module from {path}")

    module = importlib.util.module_from_spec(spec)
    # Registered before executing, so a module that imports itself resolves to this instance
    # rather than loading a second time.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_mock_module() -> ModuleType:
    """Load ``mock-ticketing/app.py`` as a fresh module under a unique name."""
    if not (MOCK_DIR / "app.py").is_file():  # pragma: no cover - defensive
        raise RuntimeError(
            f"the mock is not at {MOCK_DIR}. This suite needs the starter kit's mock, which "
            f"lives in the repository root and is not installed as a package."
        )

    if str(MOCK_DIR) not in sys.path:
        sys.path.insert(0, str(MOCK_DIR))

    # ``app.py`` does ``from seed import SEED``. Loading seed.py under the plain name ``seed``
    # makes that resolve regardless of what else is on sys.path.
    if "seed" not in sys.modules:
        sys.modules["seed"] = _import_file(MOCK_DIR / "seed.py", "seed")

    return _import_file(MOCK_DIR / "app.py", f"_mock_ticketing_app_{next(_MODULE_COUNTER)}")


def _free_port() -> int:
    """A port nothing is listening on, chosen by the OS.

    Binding to port 0 and reading the assignment back is the only way to be told a port is
    free rather than hoping one is. There is a race between closing this socket and uvicorn
    binding it; it is small, and the alternative — a hard-coded port — fails whenever
    something else on the machine already holds it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _await_ready(base_url: str) -> None:
    """Block until the mock answers, or fail with why it never did."""
    deadline = time.monotonic() + _READY_TIMEOUT_S
    last_error: BaseException | None = None

    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=0.5).status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.05)

    raise RuntimeError(f"the mock never became ready at {base_url}: {last_error!r}")


@pytest.fixture
def serve_mock(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., ServedMock]]:
    """Start real mocks on free ports, and stop them when the test is over.

    A factory rather than a single value because the scenarios here need *different* mocks:
    one that fails every call, and one that has recovered. Environment is applied **before**
    the module is imported, because the mock reads it at import time — a test that set
    ``FLAKY`` afterwards would get a healthy mock and pass for the wrong reason.
    """
    running: list[tuple[uvicorn.Server, threading.Thread]] = []

    def _serve(**env: str) -> ServedMock:
        for key, value in {**_DEFAULT_ENV, **env}.items():
            monkeypatch.setenv(key, value)

        module = _load_mock_module()
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"

        server = uvicorn.Server(
            uvicorn.Config(
                cast(Any, module).app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
            )
        )
        thread = threading.Thread(target=server.run, name=f"mock-{port}", daemon=True)
        thread.start()
        running.append((server, thread))

        _await_ready(base_url)
        return ServedMock(base_url=base_url, module=module)

    try:
        yield _serve
    finally:
        # Asked to stop first, then joined: a server thread left running would hold the port
        # and leak into the next test, which is the failure mode that makes an integration
        # suite flaky rather than slow.
        for server, _ in running:
            server.should_exit = True
        for _, thread in running:
            thread.join(timeout=_SHUTDOWN_TIMEOUT_S)
