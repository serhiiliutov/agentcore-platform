"""Fixtures for the ticketing-mock contract suite.

The mock lives in ``mock-ticketing/`` as a small standalone FastAPI app — the
starter kit's own code, which must not be modified. It is not an importable
package (the directory name contains a hyphen, and ``app.py`` does
``from seed import SEED``), so it is loaded from its file path here.

Two properties are essential for these tests to mean anything:

* **Environment is applied before import.** The mock reads ``API_KEY``, ``FLAKY``,
  ``FLAKY_RATE``, ``FLAKY_SEED`` and ``LATENCY_MS`` at module import time. A test
  that wants a flaky mock must set the variables *before* the module is loaded, so
  every load gets a fresh module instance under a unique name instead of reusing a
  cached one whose configuration was already baked in.
* **A fresh database per load.** ``app.py`` calls its own ``_reset()`` at import,
  so each loaded module starts from the 24 seeded tickets.

These tests describe the mock's real behaviour: status codes, JSON error shapes,
headers, pagination arithmetic and the chaos switch. They are the evidence the MCP
server's error taxonomy is derived from, rather than a guess at HTTP semantics
made later.
"""

from __future__ import annotations

import importlib.util
import itertools
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
MOCK_DIR = REPO_ROOT / "mock-ticketing"

#: The mock's default API key, taken from its own source.
API_KEY = "dev-key-123"

#: The seed holds TCK-1001..TCK-1024 and the counter starts at 1000 + len(SEED),
#: so the first ticket created at runtime is TCK-1025.
SEED_TICKET_COUNT = 24
FIRST_CREATED_TICKET_ID = "TCK-1025"

#: Seeded tickets used repeatedly by the suite.
TICKET_WITH_COMMENTS = "TCK-1002"
TICKET_WITHOUT_COMMENTS = "TCK-1003"

#: Every load gets its own module name, so two tests can load the same file with
#: different environment variables without interfering with each other.
_MODULE_COUNTER = itertools.count()

#: Environment the mock reads at import, with the values a hermetic test wants.
_DEFAULT_ENV: dict[str, str] = {
    "API_KEY": API_KEY,
    "FLAKY": "0",
    "FLAKY_RATE": "0.3",
    "FLAKY_SEED": "",
    "LATENCY_MS": "0",
}


#: A decoded JSON object from an HTTP response.
#:
#: ``Any`` is deliberate and narrow: these tests assert on the shape of a
#: third-party response, so enumerating every field in a ``TypedDict`` here would
#: restate the mock's schema instead of testing it. Naming the alias keeps that
#: decision visible and greppable rather than scattered as bare annotations.
JsonObject = dict[str, Any]


def auth_headers(key: str = API_KEY) -> dict[str, str]:
    """Headers for a request to one of the mock's protected routes."""
    return {"X-API-Key": key}


def _import_file(path: Path, module_name: str) -> ModuleType:
    """Import a module from an explicit file path under a chosen name."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load a module from {path}")

    module = importlib.util.module_from_spec(spec)
    # Register before executing, so a module that imports itself (directly or
    # transitively) resolves to this instance rather than loading twice.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_mock_module() -> ModuleType:
    """Load ``mock-ticketing/app.py`` as a fresh module with a unique name."""
    if str(MOCK_DIR) not in sys.path:
        sys.path.insert(0, str(MOCK_DIR))

    # ``app.py`` does ``from seed import SEED``. Loading seed.py under the name
    # ``seed`` makes that resolve regardless of what else is on sys.path.
    if "seed" not in sys.modules:
        sys.modules["seed"] = _import_file(MOCK_DIR / "seed.py", "seed")

    return _import_file(MOCK_DIR / "app.py", f"_mock_ticketing_app_{next(_MODULE_COUNTER)}")


@pytest.fixture
def load_app(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FastAPI]:
    """Load the mock's app with the given environment.

    Usage::

        app = load_app()                                       # default, healthy
        app = load_app(FLAKY="1", FLAKY_RATE="1.0")            # every call fails
    """

    def _load(**env: str) -> FastAPI:
        for key, value in {**_DEFAULT_ENV, **env}.items():
            monkeypatch.setenv(key, value)
        app: FastAPI = _load_mock_module().app
        return app

    return _load


@pytest.fixture
def client(load_app: Callable[..., FastAPI]) -> TestClient:
    """A client for a healthy mock: chaos disabled, no artificial latency."""
    return TestClient(load_app())


@pytest.fixture
def flaky_client(load_app: Callable[..., FastAPI]) -> TestClient:
    """A client whose every protected call is guaranteed to fail.

    ``FLAKY_RATE=1.0`` forces the failure decision to be taken on every request,
    and ``FLAKY_SEED`` makes the 429-vs-500 choice deterministic, so the suite can
    assert on chaos without becoming flaky itself.
    """
    return TestClient(load_app(FLAKY="1", FLAKY_RATE="1.0", FLAKY_SEED="42"))


@dataclass(frozen=True)
class MockContract:
    """The facts about the mock that every contract test needs.

    Passed as a fixture rather than imported from this module, because pytest's
    importlib mode does not guarantee that a sibling import inside a test directory
    resolves. Keeping the values on one object also means a test reads as prose:
    ``mock.first_created_ticket_id``, not a bare literal.
    """

    api_key: str = API_KEY
    seed_ticket_count: int = SEED_TICKET_COUNT
    first_created_ticket_id: str = FIRST_CREATED_TICKET_ID
    ticket_with_comments: str = TICKET_WITH_COMMENTS
    ticket_without_comments: str = TICKET_WITHOUT_COMMENTS

    def auth_headers(self, key: str | None = None) -> dict[str, str]:
        """Headers for a protected route.

        Pass ``key`` to deliberately present a wrong credential.
        """
        return {"X-API-Key": self.api_key if key is None else key}


@pytest.fixture
def mock() -> MockContract:
    """The recorded facts about the ticketing mock."""
    return MockContract()
