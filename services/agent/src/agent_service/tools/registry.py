"""Which tools the agent may call, and which of them need a human first.

This module exists for one branch. The loop, given a tool call, must choose between two
otherwise identical actions — execute it now, or record it as a pending approval — and the
registry is where that choice is *answered* rather than *made*.

**The answer is derived, not configured.** ``requires_approval`` comes from `McpTool.is_write`,
which is itself derived from the tool's published schema requiring ``client_request_id``. That
chain matters: the same fact the MCP server enforces, and that its own tests assert, is the
fact the agent's gate branches on. A hand-maintained list of "dangerous tools" would be a
second source of truth, and the two would drift the moment someone added a write tool to the
server.

**It is built from a connected client, not from a constant.** The registry cannot be
constructed without the tools the server actually published, so a tool the server does not
offer cannot be registered, and a tool it adds appears with no change here.

**A registered tool is not a callable one.** The registry says what may be *asked for*. The
only code that performs a call is the loop, and for a write it does not perform it at all until
an approval row says it may. Keeping that split means no other module can acquire the ability
to write merely by holding a registry.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agent_service.mcp.client import McpTool
from agent_service.providers.base import ToolSchema


class _Discoverable(Protocol):
    """The one thing the registry needs: something that has published its tools.

    A Protocol rather than the concrete client, for the reason Stages 4 and 6 established — a
    test can build a registry from a list without standing up a session, while the real client
    is still checked against the same shape.
    """

    @property
    def tools(self) -> tuple[McpTool, ...]:
        """The tools the server published, in its order."""
        ...


class UnknownToolError(LookupError):
    """A name was requested that the registry does not hold.

    Deliberately not the client's error of the same name: reaching the *client* with a bad name
    means something bypassed the registry, which is a different and worse situation than the
    loop looking up a name the server never offered.
    """


@dataclass(frozen=True)
class RegisteredTool:
    """A tool the agent may call, and whether it may do so unattended."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    #: When true, a call is recorded as a pending approval instead of being executed.
    requires_approval: bool

    @property
    def is_read_only(self) -> bool:
        """The complement, named so a call site reads as intent rather than as a negation."""
        return not self.requires_approval


class ToolRegistry:
    """The tools this service will call, keyed by name."""

    def __init__(self, tools: Sequence[RegisteredTool]) -> None:
        self._tools = tuple(tools)
        self._by_name = {tool.name: tool for tool in self._tools}

    @classmethod
    def from_discoverable(cls, source: _Discoverable) -> ToolRegistry:
        """Build the registry from whatever a connected client published.

        The single conversion from a published tool to a registered one. ``requires_approval``
        is read from the schema-derived ``is_write`` rather than re-derived here, so there is
        exactly one place in the service that decides what a write is.
        """
        return cls(
            [
                RegisteredTool(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    requires_approval=tool.is_write,
                )
                for tool in source.tools
            ]
        )

    @property
    def tools(self) -> tuple[RegisteredTool, ...]:
        """Every tool, in the order the server published them."""
        return self._tools

    @property
    def write_tools(self) -> tuple[RegisteredTool, ...]:
        """The ones a human must allow before they run."""
        return tuple(tool for tool in self._tools if tool.requires_approval)

    def get(self, name: str) -> RegisteredTool:
        """Look up a tool, or raise `UnknownToolError` naming what is available."""
        try:
            return self._by_name[name]
        except KeyError as exc:
            known = ", ".join(sorted(self._by_name)) or "(none)"
            raise UnknownToolError(
                f"no registered tool named {name!r}; registered: {known}"
            ) from exc

    def requires_approval(self, name: str) -> bool:
        """Whether a call to ``name`` must be approved first.

        Raises for an unknown name rather than returning a default. A default of ``False``
        would let a typo or a server change turn a write into an unattended one — the failure
        that matters most here, and the one a default would hide.
        """
        return self.get(name).requires_approval

    def as_provider_tools(self) -> tuple[ToolSchema, ...]:
        """The registry in the shape a model provider expects.

        Descriptions pass through verbatim, because the MCP server wrote them for a model to
        read and Stage 5 put the semantics there deliberately — restating them here would give
        the model two versions that could disagree.
        """
        return tuple(
            ToolSchema(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
            )
            for tool in self._tools
        )

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def __iter__(self) -> Iterator[RegisteredTool]:
        return iter(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:
        writes = ", ".join(tool.name for tool in self.write_tools) or "(none)"
        return f"ToolRegistry({len(self._tools)} tools; writes: {writes})"
