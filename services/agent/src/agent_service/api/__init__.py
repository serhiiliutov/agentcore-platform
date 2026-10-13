"""The HTTP surface: the AgentCore Runtime contract, plus a namespaced convenience API.

Two routes are the contract — ``POST /invocations`` and ``GET /ping`` — and they are what
the platform actually calls. Everything else this service serves lives under ``/api`` and
exists only for the web UI. Keeping them in separate modules is deliberate: a change to a
local convenience endpoint must never be able to alter the shape of the contract.
"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
