"""Configuration for the agent service.

Every value comes from the environment; there is no configuration file. Two rules the
rest of the service depends on:

1. **Credentials are never plain strings.** Provider keys are ``SecretStr``, so a
   ``repr()`` of the settings — in a log line, an exception, a trace span or a debug
   print — shows ``**********`` instead of the key. Making that the type's job means
   it cannot be forgotten at one call site.
2. **Nothing falls back to a real service.** The default model provider is the
   deterministic ``scripted`` one and the default database is a local file, so a
   developer who sets nothing gets a working, credential-free stack rather than a
   surprise bill or an accidental connection to production.

Settings are read once per process and cached; ``clear_settings_cache()`` exists so
tests can change the environment and re-read it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Providers this service can drive. A Literal so a typo is a startup error.
ProviderName = Literal["scripted", "replay", "anthropic", "openai", "openai_compatible", "bedrock"]

#: Transports for reaching the ticketing MCP server.
McpTransport = Literal["stdio", "http"]

#: Where a recording is expected when ``AGENT_MODEL_ID`` names a directory rather than a file.
#:
#: Defined here, in the module with no dependencies, even though `providers/replay.py` is where it
#: is used — because `Settings.configured_providers` must answer "is a recording configured?" in
#: order to decide whether `replay` is on offer at all, and that answer has to resolve a directory
#: exactly the way the provider does. Two copies of this name would be two chances to answer it
#: differently. `replay.py` imports it from here, so the dependency still points one way.
REPLAY_DEFAULT_FILENAME = "provider-recording.json"


class Settings(BaseSettings):
    """Environment-driven configuration for the agent service."""

    model_config = SettingsConfigDict(
        env_file=None,  # compose and the shell own the environment; see .env.example
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- HTTP surface (the AgentCore contract) -----------------------------
    agent_host: str = "0.0.0.0"
    agent_port: int = Field(default=8080, ge=1, le=65535)

    # --- Model provider ----------------------------------------------------
    agent_model_provider: ProviderName = "scripted"
    #: Empty means "use the provider's own default model".
    agent_model_id: str = ""

    # --- Orchestration budgets --------------------------------------------
    #: Hard ceiling on model turns in one invocation. A loop that cannot terminate
    #: is a bug; a loop that terminates with an explanation is a feature.
    agent_max_steps: int = Field(default=10, ge=1, le=100)
    #: Wall-clock ceiling for one ``/invocations`` request.
    agent_request_deadline_s: float = Field(default=90.0, gt=0)
    #: How long a proposed write stays approvable before it expires.
    agent_approval_ttl_s: int = Field(default=1800, ge=1)

    # --- Memory ------------------------------------------------------------
    #: Turns replayed verbatim before the older ones are summarised.
    agent_history_max_turns: int = Field(default=20, ge=1)
    #: How long a cross-session user fact stays valid.
    agent_memory_fact_ttl_days: int = Field(default=180, ge=1)

    # --- Persistence -------------------------------------------------------
    #: Any SQLAlchemy URL. The default is a local file so a bare checkout works;
    #: compose overrides it with the mounted volume.
    database_url: str = "sqlite+pysqlite:///./data/agent.sqlite3"

    # --- Observability -----------------------------------------------------
    trace_dir: Path = Path("./data/traces")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "agentcore-platform"

    # --- The ticketing MCP server -----------------------------------------
    mcp_transport: McpTransport = "stdio"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8081, ge=1, le=65535)
    #: Shared secret this service presents to the MCP server over HTTP.
    mcp_token: SecretStr | None = None
    #: How to launch the MCP server when the transport is ``stdio``. A single string
    #: that the client splits, because that is what an operator writes in a compose
    #: file, and requiring a JSON array for a two-word command would be worse.
    mcp_stdio_command: str = "python -m ticketing_mcp"
    #: Name the server is expected to report. Checked after initialize so a
    #: misconfigured command fails with a clear message instead of at the first call.
    mcp_expected_server_name: str = "ticketing"

    # --- Provider credentials (all optional) -------------------------------
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    #: An OpenAI-shaped endpoint: Ollama, vLLM, LM Studio, Groq, Together...
    openai_base_url: str = ""
    aws_region: str = ""
    bedrock_model_id: str = ""

    @field_validator("trace_dir", mode="before")
    @classmethod
    def _expand_trace_dir(cls, value: object) -> object:
        """Accept ``~`` in a path from the environment, as a shell would."""
        if isinstance(value, str):
            return Path(value).expanduser()
        return value

    @field_validator("database_url")
    @classmethod
    def _require_a_database_scheme(cls, value: str) -> str:
        """Fail at startup rather than on the first query."""
        if "://" not in value:
            raise ValueError(
                "DATABASE_URL must be a full SQLAlchemy URL, e.g. "
                "sqlite+pysqlite:///./data/agent.sqlite3"
            )
        return value

    @field_validator("anthropic_api_key", "openai_api_key", mode="before")
    @classmethod
    def _a_blank_credential_is_no_credential(cls, value: object) -> object:
        """Treat an empty or whitespace-only key as absent rather than as a key.

        Compose passes ``${ANTHROPIC_API_KEY:-}``, so a shell that has not exported the variable
        delivers an empty *string* rather than nothing at all — and ``SecretStr("")`` is not
        ``None``, which is what every ``is not None`` test in this file read it as.

        The cost was real, and was found by comparing two things that disagreed: ``/api/info``
        listed ``anthropic`` and ``openai`` as providers this process could use while the
        container's own environment held neither key. What that meant underneath is worse than a
        misleading listing — ``selected_provider_is_usable()`` returned True for a provider whose
        key was blank, so a service started with ``AGENT_MODEL_PROVIDER=anthropic`` and an empty
        key was *not* refused at startup the way README section c promises, and would instead
        fail on the first model call. A blank key has to be blank for that check to mean anything.

        ``mcp_token`` is deliberately not listed here: its one reader already tests truthiness
        (``if not settings.mcp_token``), so a blank token is refused there with a message about
        the transport — no change is needed for that, and adding one would hide the fact.

        Both shapes of "blank" are handled, because this runs *before* the type is coerced and so
        can be handed either one: the environment and compose supply a plain ``str``, while a
        caller constructing ``Settings`` directly supplies a ``SecretStr``. Unwrapping the second is
        not hypothetical tidiness — without it a blank ``SecretStr`` is not a blank ``str``, and the
        value would be read as a configured key by the same ``is not None`` test this exists to
        correct. It was found the direct way: the first version of this validator passed its own
        tests and a typed caller still got the old behaviour.
        """
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # --- Derived state -----------------------------------------------------
    @property
    def is_sqlite(self) -> bool:
        """SQLite needs different connect arguments and a WAL pragma."""
        return self.database_url.startswith("sqlite")

    def configured_providers(self) -> tuple[ProviderName, ...]:
        """Providers that could actually be used right now.

        Used to refuse an unusable selection at startup and to name the alternatives in
        the resulting message. No endpoint publishes it, so nothing outside this process
        sees it, and it returns names only: no key material, no key prefix, no hint about
        a key's contents.
        """
        available: list[ProviderName] = ["scripted"]

        # `replay` is conditional, exactly as the three live providers below are, and the reason is
        # the principle this method exists to serve: the picker must not offer a choice that cannot
        # be built. `replay` reads as unconditional and is not — its "model id" is the path to a
        # recording, so with none configured it raises "the replay provider needs a recording" the
        # instant it is selected. That is not hypothetical: a reader saw `replay` in the dropdown,
        # chose it, and got "recording not found: gpt-4o-mini", because the settings panel sent the
        # *previous* provider's model id along with the new provider's name. A provider whose only
        # requirement is absent is absent.
        if self.replay_recording_is_configured():
            available.append("replay")

        if self.anthropic_api_key is not None:
            available.append("anthropic")
        if self.openai_api_key is not None:
            available.append("openai")
        if self.openai_base_url:
            # A base URL is all an OpenAI-shaped local endpoint needs.
            available.append("openai_compatible")
        if self.aws_region:
            available.append("bedrock")

        return tuple(available)

    def replay_recording_is_configured(self) -> bool:
        """Whether ``AGENT_MODEL_ID`` points at a recording that is actually there.

        Existence rather than a non-empty string, because that is the whole difference between
        "the picker offers `replay`" and "choosing `replay` works": `ReplayProvider.from_settings`
        resolves a directory to `REPLAY_DEFAULT_FILENAME` and refuses a path that is not present,
        so a typo in the setting would otherwise be offered and then rejected on use. The
        resolution below mirrors the provider's own rather than approximating it — a directory is
        as good as the file inside it, because that is the file the provider will load.
        """
        raw = self.agent_model_id.strip()
        if not raw:
            return False
        path = Path(raw)
        if path.is_dir():
            path = path / REPLAY_DEFAULT_FILENAME
        return path.exists()

    def selected_provider_is_usable(self) -> bool:
        """Whether the configured provider has what it needs to run.

        Checked at startup so a missing key is a clear message rather than a
        confusing failure three turns into a conversation.
        """
        return self.agent_model_provider in self.configured_providers()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read the environment once and cache the result."""
    return Settings()


def clear_settings_cache() -> None:
    """Drop the cached settings. For tests that change the environment."""
    get_settings.cache_clear()
