"""``python -m eval`` — the entry point ``make eval`` calls.

Deliberately four lines. Everything worth reading is in `runner.py`; a `__main__` that grew logic
would be a second place where "did the run pass" is decided, and the exit code has to come from
exactly one of them.

The exit code is the contract: 0 when every case passed, 1 when any did not, so the same command
serves both a person reading a table and CI deciding whether to fail a build.
"""

from __future__ import annotations

from eval.runner import main

if __name__ == "__main__":
    raise SystemExit(main())
