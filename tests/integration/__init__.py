"""Tests that drive real processes over real sockets.

Everything in `tests/unit/` is hermetic by design: fakes in, assertions out, no port, no
subprocess. That is the right default, and it is also the reason this package exists — a suite
of fakes can stay green while the assembled system is broken, because a fake encodes what the
author *believed* the real thing does.

The tests here are the ones that would notice. They start a real mock over HTTP and drive the
real client, its real retry policy, and a real idempotency ledger against it, which is the only
way to prove claims that are about HTTP behaviour rather than about arithmetic: that a retry
budget is exhausted rather than honoured forever, that a failure storm leaves the backend
untouched, and that a write repeated after a failure produces one ticket rather than two.
"""
