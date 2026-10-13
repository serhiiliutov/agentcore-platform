"""Shared pytest configuration for the whole repository.

Two jobs:

1. **Keep credentials out of CI.** Tests marked ``live`` talk to a real model
   provider. They are deselected unless ``--run-live`` is passed, so the default
   run needs no API key and no network.
2. **Keep the default run honest about what it needs.** Tests marked
   ``integration`` need the stack up; the default unit-test command excludes them
   via `-m`. This file only enforces the ``live`` gate, because that one is a
   safety property rather than a speed one.

Service-level tests live next to the code they test
(``services/*/tests``) and inherit this file's behaviour.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add the opt-in flags this repository uses."""
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="run tests marked `live`, which call a real model provider and need credentials",
    )
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="run tests marked `slow`, which take more than a few seconds",
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: Sequence[pytest.Item],
) -> None:
    """Deselect `live` and `slow` tests unless explicitly asked for.

    The skip carries a reason so that a CI log or a reviewer running pytest by hand
    can see that tests were skipped on purpose rather than silently not collected.
    """
    if not config.getoption("--run-live"):
        skip_live = pytest.mark.skip(reason="needs --run-live and a real provider credential")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip_live)

    if not config.getoption("--run-slow"):
        skip_slow = pytest.mark.skip(reason="needs --run-slow")
        for item in items:
            if "slow" in item.keywords:
                item.add_marker(skip_slow)
