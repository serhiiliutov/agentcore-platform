"""Model providers behind one narrow interface (ADR-0002).

The loop speaks only the types in `base.py`. Each provider translates them to and from
whatever its SDK wants, which is why adding a provider is one file and one registry entry
rather than a change to the orchestration.

`scripted` and `replay` are not fallbacks for a missing key. They are the providers the test
suite and the eval harness run on, which is what makes results reproducible and free — a live
model cannot give the same answer twice, so it cannot detect a regression in the loop.
"""

from __future__ import annotations

from agent_service.config import ProviderName

from .base import (
    AssistantTurn,
    Message,
    ModelProvider,
    ProviderNotConfiguredError,
    Role,
    StopReason,
    ToolCallRequest,
    ToolSchema,
    Usage,
    build_provider,
)

__all__ = [
    "AssistantTurn",
    "Message",
    "ModelProvider",
    "ProviderName",
    "ProviderNotConfiguredError",
    "Role",
    "StopReason",
    "ToolCallRequest",
    "ToolSchema",
    "Usage",
    "build_provider",
]
