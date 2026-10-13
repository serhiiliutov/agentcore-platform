"""Fixtures for the unit suite.

Deliberately thin. The fake itself lives in `fakes.py` so a test module can import it
directly; keeping it here would mean importing a pytest-internal module from a test file,
which works until it does not.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.unit.fakes import FakeTicketing, make_ticket
from ticketing_mcp.idempotency import IdempotencyLedger
from ticketing_mcp.models import NewTicket


@pytest.fixture
def transport() -> FakeTicketing:
    """A fresh scripted API for each test."""
    return FakeTicketing()


@pytest.fixture
def ledger() -> IdempotencyLedger:
    """An in-memory ledger, so no test touches the filesystem."""
    return IdempotencyLedger(":memory:")


@pytest.fixture
def new_ticket() -> Callable[..., NewTicket]:
    """The ticket factory, as a fixture so tests do not import across modules."""
    return make_ticket
