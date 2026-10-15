"""Which model the eval runs on, and the two ways that can be got wrong.

Both failure modes are silent, which is why they are tested rather than trusted:

1. **A run that names a live model and quietly uses `scripted`.** That produces a result file
   claiming a model was exercised, when the numbers describe the orchestration — the file is
   committed as evidence, so the claim it makes has to be the claim it earned.
2. **A run that cannot build the provider it named, and fails somewhere further in.** A missing
   credential has to be a sentence at the start of the run, not a 401 inside one case's payload
   twenty minutes later.

The provider's name is passed in rather than read from `os.environ`, so these tests cannot be
affected by — or accidentally affect — the developer's own shell.
"""

from __future__ import annotations

import pytest
from eval.provider import (
    DEFAULT_PROVIDER,
    PROVIDER_ENV,
    ProviderError,
    resolve_provider,
)

#: A stand-in credential. Never a real one, and never one that looks usable, so a failure message
#: quoting it back would be obvious in the test output rather than alarming.
FAKE_KEY = "test-key-not-a-real-credential"


def test_the_default_needs_no_credential_and_calls_no_model() -> None:
    """A bare checkout runs the suite. That is the property the whole eval story rests on."""
    provider = resolve_provider({})

    assert provider.name == DEFAULT_PROVIDER
    assert not provider.is_live
    assert provider.env == {"AGENT_MODEL_PROVIDER": DEFAULT_PROVIDER}


def test_a_live_provider_is_handed_to_the_agent_with_its_credential() -> None:
    provider = resolve_provider({PROVIDER_ENV: "openai", "OPENAI_API_KEY": FAKE_KEY})

    assert provider.is_live
    assert provider.env["AGENT_MODEL_PROVIDER"] == "openai"
    assert provider.env["OPENAI_API_KEY"] == FAKE_KEY


def test_each_provider_gets_its_own_credential_variable() -> None:
    """The mapping is per-provider, so `anthropic` must not be satisfied by an OpenAI key."""
    anthropic = resolve_provider({PROVIDER_ENV: "anthropic", "ANTHROPIC_API_KEY": FAKE_KEY})
    assert anthropic.env["ANTHROPIC_API_KEY"] == FAKE_KEY

    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
        resolve_provider({PROVIDER_ENV: "anthropic", "OPENAI_API_KEY": FAKE_KEY})


def test_a_missing_credential_is_refused_by_name() -> None:
    """The sentence has to name the variable, or the reader has to go and read the source."""
    with pytest.raises(ProviderError) as raised:
        resolve_provider({PROVIDER_ENV: "openai"})

    message = str(raised.value)
    assert "OPENAI_API_KEY" in message
    assert "scripted" in message, "the message should say what to run instead"


def test_a_whitespace_only_credential_counts_as_missing() -> None:
    """Compose passes `${VAR:-}`, so an unset variable arrives as an empty *string*.

    The agent's own settings settle this the same way (`Settings._a_blank_credential_is_no_credential`),
    and the two have to agree: a suite that accepted `"   "` as a key would build a provider the
    agent then refuses at startup — reporting the mistake at the wrong end of the run.
    """
    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        resolve_provider({PROVIDER_ENV: "openai", "OPENAI_API_KEY": "   "})


def test_an_unknown_provider_is_refused_with_the_alternatives() -> None:
    """A typo must not fall back to the default: that would measure the wrong thing and say so."""
    with pytest.raises(ProviderError) as raised:
        resolve_provider({PROVIDER_ENV: "opneai"})

    assert "opneai" in str(raised.value)
    assert "openai" in str(raised.value)


def test_a_blank_provider_falls_back_to_the_default() -> None:
    """An empty value is "not configured", not a provider named empty-string."""
    assert resolve_provider({PROVIDER_ENV: "  "}).name == DEFAULT_PROVIDER


def test_openai_compatible_needs_an_address_rather_than_a_key() -> None:
    """The local-model path: no credential, but a base URL to point at."""
    provider = resolve_provider(
        {PROVIDER_ENV: "openai_compatible", "OPENAI_BASE_URL": "http://localhost:11434/v1"}
    )

    assert provider.is_live
    assert provider.env["OPENAI_BASE_URL"] == "http://localhost:11434/v1"
    assert "OPENAI_API_KEY" not in provider.env

    with pytest.raises(ProviderError, match="OPENAI_BASE_URL"):
        resolve_provider({PROVIDER_ENV: "openai_compatible"})


def test_bedrock_needs_a_region_and_not_a_key() -> None:
    """boto3 takes its credentials from its own chain, so demanding a key here would refuse a
    correctly configured machine."""
    provider = resolve_provider({PROVIDER_ENV: "bedrock", "AWS_REGION": "eu-central-1"})

    assert provider.env["AWS_REGION"] == "eu-central-1"
    assert provider.is_live

    with pytest.raises(ProviderError, match="AWS_REGION"):
        resolve_provider({PROVIDER_ENV: "bedrock"})


def test_the_description_names_the_kind_of_run_and_never_the_credential() -> None:
    """This string is printed and could be pasted into a report. It must carry no secret."""
    live = resolve_provider({PROVIDER_ENV: "openai", "OPENAI_API_KEY": FAKE_KEY})

    assert FAKE_KEY not in live.describe()
    assert "live" in live.describe()
    assert "deterministic" in resolve_provider({}).describe()


def test_the_env_mapping_is_the_only_thing_that_carries_the_credential() -> None:
    """The credential is in `env` because the agent process needs it, and nowhere else.

    Asserted as a shape rather than as a string search, because the point is that the dataclass
    has no other field a credential could hide in — a report, a log line or a JSON payload built
    from it would have to reach into `env` deliberately to leak.
    """
    provider = resolve_provider({PROVIDER_ENV: "openai", "OPENAI_API_KEY": FAKE_KEY})

    assert set(vars(provider)) == {"name", "env"}
    assert FAKE_KEY in provider.env.values()
