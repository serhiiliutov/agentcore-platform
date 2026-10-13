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
ProviderName = Literal[
    "scripted", "replay", "anthropic", "openai", "openai_compatible", "bedrock"
]

#: Transports for reaching the ticketing MCP server.
McpTransport = Literal["stdio", "http"]


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

    # --- Provider credentials (all optional) -------------------------------
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    #: An OpenAI-shaped endpoint: Ollama, vLLM, LM Studio, Groq, Together...
    openai_base_url: str = ""
    aws_region: str = ""
    bedrock_model_id: str = ""

    # --- Development-only: accept a key from the web UI --------------------
    #: Off by default. When on, a key may be submitted at runtime; it is held in
    #: memory only and is documented as development-only.
    allow_runtime_key_entry: bool = False

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

    # --- Derived state -----------------------------------------------------
    @property
    def is_sqlite(self) -> bool:
        """SQLite needs different connect arguments and a WAL pragma."""
        return self.database_url.startswith("sqlite")

    def configured_providers(self) -> tuple[ProviderName, ...]:
        """Providers that could actually be used right now.

        This is what the web UI is allowed to know: a provider is offered only if it
        has what it needs. No key material, no key prefix and no hint about a key's
        contents ever leaves this method.
        """
        available: list[ProviderName] = ["scripted", "replay"]

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
