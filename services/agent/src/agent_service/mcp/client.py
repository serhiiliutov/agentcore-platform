"""The MCP client: the agent's only route to the ticketing tools.

Two properties make this more than a thin wrapper.

**The tool surface is discovered, never hardcoded.** `connect` calls `initialize` then
`list_tools` and keeps what the server publishes. A tool added to the server appears here
with no change to this service, and a tool removed here cannot be called — the two cannot
disagree about what exists. That is the anti-drift property ADR-0001 claims, and hardcoding
a list of four names would quietly discard it.

**Whether a tool is a write is derived, not guessed.** A write tool in this system is one
whose schema *requires* `client_request_id` — that is the structural property the MCP server
publishes and its tests assert. Deriving it means the agent's decision about which calls need
approval comes from the same source of truth as the server's own contract, rather than from a
name-prefix heuristic that would silently classify a future `create_bulk_tickets` as a read.

Both transports are supported because both were built: `stdio` launches the server as a
subprocess, `http` reaches a running one. The only difference is how the session is opened;
everything after `initialize` is identical, which is why the transport choice does not appear
anywhere below that line.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from agent_service.config import Settings

#: The parameter whose presence marks a tool as a write.
WRITE_MARKER_PARAM = "client_request_id"

#: Default per-call ceiling. Shorter than the agent's own deadline on purpose: a single
#: tool call should fail before the whole invocation does, so the loop can report which
#: call was slow instead of timing out with nothing to say.
DEFAULT_CALL_TIMEOUT_S = 20.0


class McpClientError(Exception):
    """Anything that stops the agent from using the tools."""


class McpConnectionError(McpClientError):
    """The server could not be reached, launched, or identified.

    Raised during ``connect`` — the failure is a deployment or configuration problem, and
    finding out at startup beats finding out mid-conversation.
    """


class UnknownToolError(McpClientError):
    """A tool was called that the server does not publish.

    A programming error rather than a runtime condition: the agent may only call tools it
    discovered, so this means a name was assembled by hand somewhere it should not have
    been.
    """


@dataclass(frozen=True)
class McpTool:
    """A tool as the server publishes it.

    ``read_only`` is computed from the schema rather than carried separately, so it cannot
    drift from the schema the server actually sent.
    """

    name: str
    description: str
    input_schema: Mapping[str, Any]

    @property
    def required_params(self) -> frozenset[str]:
        """The parameters the schema marks as required, or none if it says nothing."""
        required = self.input_schema.get("required")
        if isinstance(required, Sequence) and not isinstance(required, str):
            return frozenset(str(item) for item in required)
        return frozenset()

    @property
    def is_write(self) -> bool:
        """Whether calling this tool changes something.

        A write is a tool that demands an idempotency key. That is the server's own
        structural signal — the write tools in `ticketing_mcp.server` require
        ``client_request_id``, and its tests assert it — so the agent's approval decision
        rests on the same fact the server enforces rather than on a naming convention.
        """
        return WRITE_MARKER_PARAM in self.required_params


@dataclass(frozen=True)
class ToolCallResult:
    """What a tool returned, in terms the agent can reason about.

    The server sends both a sentence and structured data; both are kept. The sentence is
    what a model reads, and the structured form is what the loop inspects to decide whether
    an error is worth retrying — parsing prose for that would be guesswork.
    """

    is_error: bool
    text: str
    structured: Mapping[str, Any] | None = None

    @property
    def message(self) -> str:
        """The server's sentence, falling back to the structured form and then to ''."""
        if self.structured is not None:
            message = self.structured.get("message")
            if isinstance(message, str) and message:
                return message
        return self.text

    @property
    def data(self) -> Mapping[str, Any]:
        """The structured payload, or an empty mapping when there is none."""
        if self.structured is None:
            return {}
        payload = self.structured.get("data")
        return payload if isinstance(payload, Mapping) else {}

    def as_model_text(self) -> str:
        """The text to place in a model's tool-result message.

        Prefixed on failure so a model reading a transcript cannot mistake an error for a
        success — the two arrive through the same channel, and only the flag distinguishes
        them.
        """
        return f"ERROR: {self.message}" if self.is_error else self.message


class TicketingMcpClient:
    """A connected session with the ticketing MCP server, plus its tool catalog.

    Constructed by `connect` rather than directly: the session has to be initialized and
    its tools listed before this is useful, and a constructor that could be called without
    doing that would invite an uninitialized client.
    """

    def __init__(
        self,
        session: ClientSession,
        tools: tuple[McpTool, ...],
        *,
        server_name: str,
    ) -> None:
        self._session = session
        self._tools = tools
        self._by_name = {tool.name: tool for tool in tools}
        self.server_name = server_name

    @property
    def tools(self) -> tuple[McpTool, ...]:
        """The catalog, in the order the server listed it."""
        return self._tools

    @property
    def write_tools(self) -> tuple[McpTool, ...]:
        """The subset that changes state, and therefore needs approval."""
        return tuple(tool for tool in self._tools if tool.is_write)

    def tool(self, name: str) -> McpTool:
        """Look up a tool, or raise `UnknownToolError`."""
        try:
            return self._by_name[name]
        except KeyError as exc:
            known = ", ".join(sorted(self._by_name)) or "(none)"
            raise UnknownToolError(
                f"the server does not publish a tool named {name!r}; known: {known}"
            ) from exc

    async def call(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> ToolCallResult:
        """Call a discovered tool and normalise the result.

        The name is checked against the catalog first. Letting the server reject an unknown
        name would work, but the error would arrive as a protocol-level failure rather than
        as "you asked for something that does not exist here", and the caller would have to
        tell those apart.

        ``read_timeout_seconds`` is passed to the session rather than enforced with a timer
        around it — Stage 3 found the SDK accepts a per-call deadline, and using it means
        the transport stops reading rather than this coroutine abandoning a live request.
        """
        self.tool(name)  # raises UnknownToolError before touching the wire

        result = await self._session.call_tool(
            name,
            dict(arguments or {}),
            read_timeout_seconds=timeout_s or DEFAULT_CALL_TIMEOUT_S,
        )
        return _normalise(result, tool_name=name)

    @classmethod
    @asynccontextmanager
    async def connect(cls, settings: Settings) -> AsyncIterator[TicketingMcpClient]:
        """Open a session, verify the server, and yield a client.

        The expectation check is not ceremony: ``mcp_stdio_command`` is operator-supplied,
        so a typo could launch a different MCP server that answers perfectly well with the
        wrong tools. Comparing the reported name turns that into one clear message instead
        of a conversation that quietly calls a stranger's tools.
        """
        try:
            async with _open_session(settings) as session:
                reported, tools = await _inspect(settings, session)
                yield cls(session, tools, server_name=reported)
        except McpClientError:
            raise
        except Exception as exc:
            raise McpConnectionError(f"could not connect to the MCP server: {exc}") from exc


async def _inspect(settings: Settings, session: ClientSession) -> tuple[str, tuple[McpTool, ...]]:
    """Initialize the session, verify it is the server we meant, and read its catalog.

    The two checks live in their own functions rather than as ``raise`` statements inside
    the caller's ``try``. Both would work, but a raise directly inside a block that catches
    broad exceptions is one edit away from being swallowed by its own handler — which is the
    failure this whole method exists to report clearly.
    """
    info = await session.initialize()
    # ``serverInfo`` is the protocol's name for this and ``server_info`` is the SDK's pydantic
    # model's, and which one is populated has already surprised this project once — ``_to_tool``
    # below carries the same guard for ``inputSchema``. Reading both and taking whichever is
    # present costs a line and removes a failure whose only symptom is an empty string, which is
    # exactly what this produced: the expectation check could never pass, for any server.
    server_info = getattr(info, "serverInfo", None) or getattr(info, "server_info", None)
    reported = str(getattr(server_info, "name", "") or "")
    _require_expected_server(settings, reported)

    listed = await session.list_tools()
    tools = tuple(_to_tool(item) for item in listed.tools)
    _require_tools(reported, tools)
    return reported, tools


def _require_expected_server(settings: Settings, reported: str) -> None:
    """Refuse a server that is not the one configured.

    ``mcp_stdio_command`` is operator-supplied, so a typo could launch a different MCP
    server that answers perfectly well with the wrong tools. Comparing the reported name
    turns that into one clear message instead of a conversation that quietly calls a
    stranger's tools.
    """
    expected = settings.mcp_expected_server_name
    if expected and reported != expected:
        raise McpConnectionError(
            f"expected MCP server {expected!r} but {reported!r} answered; "
            f"check MCP_STDIO_COMMAND or MCP_PORT"
        )


def _require_tools(reported: str, tools: tuple[McpTool, ...]) -> None:
    """Refuse a server with an empty catalog.

    An agent with no tools cannot do anything, and discovering that on the first user
    message is worse than discovering it at startup.
    """
    if not tools:
        raise McpConnectionError(
            f"MCP server {reported!r} publishes no tools; the agent would have nothing to call"
        )


@asynccontextmanager
async def _open_session(settings: Settings) -> AsyncIterator[ClientSession]:
    """Open a `ClientSession` over whichever transport the settings select.

    The only place the transport is mentioned. Both branches yield a ``(read, write)`` pair
    — `TransportStreams` is a two-tuple — so from here on the client is transport-agnostic.
    """
    if settings.mcp_transport == "stdio":
        argv = shlex.split(settings.mcp_stdio_command)
        if not argv:
            raise McpConnectionError("MCP_STDIO_COMMAND is empty; nothing to launch")
        # The child is given this process's environment in full.
        #
        # This is not incidental. ``mcp`` defaults a spawned server to a curated whitelist of
        # variables — HOME, PATH, SYSTEMROOT and so on — which is the right default for a
        # third-party server, and wrong for this one: the MCP server authenticates to the mock
        # with ``TICKETING_API_KEY`` and finds it via ``TICKETING_BASE_URL``, so under the
        # default it exits with "TICKETING_API_KEY is not set" before the handshake completes,
        # and the agent reports only "Connection closed".
        #
        # Forwarding everything rather than enumerating the variables the server needs is
        # deliberate: an allow-list here would have to be kept in step with the server's own
        # configuration, and the failure when it drifted would be this same opaque one. The
        # server is this repository's code, started by this repository's process — the trust
        # boundary the whitelist exists to enforce is not crossed by passing it the environment
        # its parent already holds.
        params = StdioServerParameters(command=argv[0], args=argv[1:], env=dict(os.environ))
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            yield session
    else:
        if not settings.mcp_token:
            # Over HTTP the server is reachable by anything that can route to the port;
            # requiring a token keeps that from being an accident.
            raise McpConnectionError(
                "MCP_TRANSPORT=http requires MCP_TOKEN so the tool server is not reachable "
                "by anything that can route to its port"
            )
        url = f"http://{settings.mcp_host}:{settings.mcp_port}/mcp"
        async with (
            streamable_http_client(url) as (read, write),
            ClientSession(read, write) as session,
        ):
            yield session


def _to_tool(item: Any) -> McpTool:
    """Adapt an SDK tool description into our own type.

    A one-way conversion rather than using the SDK object directly, so nothing downstream
    depends on the SDK's field names — Stage 5 lost two tests to `inputSchema` versus
    `input_schema`, and this contains that class of surprise to one function.
    """
    schema = getattr(item, "inputSchema", None) or getattr(item, "input_schema", None) or {}
    return McpTool(
        name=str(getattr(item, "name", "")),
        description=str(getattr(item, "description", "") or ""),
        input_schema=schema if isinstance(schema, Mapping) else {},
    )


def _normalise(result: Any, *, tool_name: str) -> ToolCallResult:
    """Flatten an SDK `CallToolResult` into ours.

    The MCP result carries a list of content blocks; only text blocks can be handed to a
    model as a tool result, so they are joined and anything else is ignored rather than
    stringified. ``structured_content`` is preserved when present.
    """
    blocks = getattr(result, "content", None) or []
    texts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            texts.append(text)

    structured = getattr(result, "structured_content", None)
    combined = "\n".join(texts).strip()

    return ToolCallResult(
        is_error=bool(getattr(result, "is_error", False)),
        # A tool that returned nothing must not read as an empty success.
        text=combined or f"the {tool_name} tool returned no text content",
        structured=structured if isinstance(structured, Mapping) else None,
    )
