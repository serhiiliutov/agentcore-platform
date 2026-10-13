"""The agent loop: the one place a turn is orchestrated.

Everything below this package is a component with one job — a provider produces a reply, a
registry says what may be called, an approval row records a proposed write. This package is
where those meet, and it is deliberately the only place that knows the order they happen in.
"""

from __future__ import annotations

from .loop import AgentLoop, LoopLimits, TurnResult

__all__ = ["AgentLoop", "LoopLimits", "TurnResult"]
