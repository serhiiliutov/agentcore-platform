"""Contract tests for the ticketing mock.

These tests pin the behaviour of the starter kit's ticketing API — the fixture this
whole project is measured against — before any code is written against it. Every
status code, JSON error shape, header and pagination rule asserted here becomes an
explicit input to the MCP server's error taxonomy, the retry policy and the
verify-after-write logic.

They run without Docker and without a credential: ``conftest.py`` loads the mock's
module in-process, with the environment applied before import so that the chaos
switch can be configured per test. A rediscovery of the mock's behaviour here costs
seconds; the same discovery made by guessing at HTTP semantics in Stage 4 would
cost a wrong retry policy.
"""
