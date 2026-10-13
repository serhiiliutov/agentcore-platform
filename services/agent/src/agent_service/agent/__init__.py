"""The agent: the loop, the gate's executor, and the contract wiring.

Everything below this package is a component with one job — a provider produces a reply, a
registry says what may be called, an approval row records a proposed write. This package is
where those meet, and it is deliberately the only place that knows the order they happen in.

Two exported things, and the split between them is the guarantee:

* `AgentLoop` **proposes**. It reads, decides, and records a write as a pending row. It has no
  code path that calls a write tool.
* `WriteExecutor` **performs**. It is the only thing in the service that can call a write tool,
  and it cannot be reached without an approved row.

Nothing else in this package can write, and nothing outside it needs to know which of the two
is which — `InvocationHandlerFactory` builds both from one provider and one executor.
"""

from __future__ import annotations

from .execution import ExecutionResult, WriteExecutor, is_runnable
from .handler import (
    STOP_REASON_MAP,
    SYSTEM_PROMPT,
    InvocationHandlerFactory,
    to_invocation_response,
)
from .loop import AgentLoop, LoopLimits, ToolCallRecord, ToolExecutor, TurnResult

__all__ = [
    "STOP_REASON_MAP",
    "SYSTEM_PROMPT",
    "AgentLoop",
    "ExecutionResult",
    "InvocationHandlerFactory",
    "LoopLimits",
    "ToolCallRecord",
    "ToolExecutor",
    "TurnResult",
    "WriteExecutor",
    "is_runnable",
    "to_invocation_response",
]
