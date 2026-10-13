"""Settings for the MCP server, read from the environment.

Kept separate from `client.TicketingConfig` because the two have different jobs. That one
says how to reach the API and is what the transport needs; this one is the *process*
configuration — where the ledger lives, what version to report — and is what `__main__`
and the container need.

The API key is a `SecretStr`. Stage 2 made the same choice for the agent service, for the
same reason: a `repr()` of the settings in a log line or a traceback shows asterisks
rather than the credential. Redaction becomes the type's job instead of a rule someone
has to remember at each call site.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from . import __version__
from .errors import ConfigurationError

#: Environment variables the server reads. Named rather than inline so a test and a
#: deployment cannot disagree about the spelling.
ENV_BASE_URL = "TICKETING_API_URL"
ENV_API_KEY = "TICKETING_API_KEY"
ENV_LEDGER_PATH = "TICKETING_LEDGER_PATH"
ENV_TIMEOUT_S = "TICKETING_TIMEOUT_S"

#: Defaults that work against the Stage 1 mock on localhost.
DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_LEDGER_PATH = "data/ticketing-idempotency.sqlite3"
DEFAULT_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class ServerSettings:
    """Everything the server process needs in order to start."""

    base_url: str
    api_key: SecretStr
    ledger_path: Path
    timeout_s: float
    #: Reported over MCP so a client can tell which build it is talking to.
    version: str = __version__

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ServerSettings:
        """Build settings from the environment, failing loudly on a missing key.

        A server that starts without a credential and only fails on the first tool call
        looks healthy to a supervisor and broken to a user. Refusing to start is the
        clearer failure.
        """
        source = env if env is not None else dict(os.environ)

        api_key = source.get(ENV_API_KEY, "").strip()
        if not api_key:
            raise ConfigurationError(
                f"{ENV_API_KEY} is not set. The server cannot authenticate to the "
                f"ticketing API and will not start without it."
            )

        raw_timeout = source.get(ENV_TIMEOUT_S, "").strip()
        try:
            timeout_s = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_S
        except ValueError as exc:
            raise ConfigurationError(
                f"{ENV_TIMEOUT_S} must be a number, got {raw_timeout!r}"
            ) from exc
        if timeout_s <= 0:
            raise ConfigurationError(f"{ENV_TIMEOUT_S} must be positive, got {timeout_s}")

        return cls(
            base_url=source.get(ENV_BASE_URL, DEFAULT_BASE_URL).rstrip("/"),
            api_key=SecretStr(api_key),
            ledger_path=Path(source.get(ENV_LEDGER_PATH, DEFAULT_LEDGER_PATH)),
            timeout_s=timeout_s,
        )
