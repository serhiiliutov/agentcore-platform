"""The tool registry: discovered tools plus the one fact the loop needs about each.

A tool's schema says how to call it. It does not say whether calling it is safe to do without
asking a human, and that is the single fact the loop branches on. The registry is where that
fact is attached, derived from the schema rather than configured — see `registry.py`.
"""

from __future__ import annotations

from .registry import RegisteredTool, ToolRegistry

__all__ = ["RegisteredTool", "ToolRegistry"]
