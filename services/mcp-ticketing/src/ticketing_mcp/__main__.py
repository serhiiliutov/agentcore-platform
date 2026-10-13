"""Entry point: `python -m ticketing_mcp [--transport ...]`.

One process, two transports, chosen on the command line. ADR-0001 committed to shipping
both because stdio is what an MCP client launches locally and streamable HTTP is what a
container exposes, and having a single entry point means the tool definitions are written
once for either.

A missing credential is reported as a clean message and exit code 2, not a traceback. That
distinction matters in a container: a supervisor should see "the server refused to start
because TICKETING_API_KEY was not set", not a stack of Python frames that looks like a bug
in the server itself.
"""

from __future__ import annotations

import argparse
import sys

from .config import ServerSettings
from .errors import ConfigurationError
from .server import build_server

#: Exit code for "the process is misconfigured", as distinct from "the process crashed".
EXIT_CONFIGURATION_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ticketing-mcp",
        description="An MCP server exposing the ticketing API as four tools.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="stdio for a locally launched client; streamable-http for a container.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address for streamable-http. Ignored for stdio.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Bind port for streamable-http. Ignored for stdio.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = ServerSettings.from_env()
    except ConfigurationError as exc:
        # Deliberately not a traceback: this is a deployment mistake, and the message is
        # the useful part.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIGURATION_ERROR

    server, ticketing = build_server(settings)

    if args.transport == "stdio":
        # stdout is the MCP channel over stdio, so nothing may be printed to it here.
        server.run("stdio")
    else:
        try:
            server.run("streamable-http", host=args.host, port=args.port)
        finally:
            # Only meaningful on the HTTP path, where the process could in principle
            # outlive the call; the stdio path ends with the process.
            del ticketing

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
