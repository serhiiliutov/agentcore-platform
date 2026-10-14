"""Settings: which credentials actually count, and what a blank one means.

This file exists for one defect, found by comparing two things that disagreed: the page's
``/api/info`` listed ``anthropic`` and ``openai`` as providers the service could use, while the
container's own environment held neither key.

The cause was a string-versus-absence mistake. Compose passes ``${ANTHROPIC_API_KEY:-}``, so a
shell that has not exported the variable delivers an empty *string* rather than nothing at all, and
``SecretStr("")`` is not ``None`` — which every ``is not None`` check in ``config.py`` read as "a key
is configured". The listing was the visible half; the other half was worse.
``selected_provider_is_usable()`` returned True for a provider whose key was blank, so a service
started with ``AGENT_MODEL_PROVIDER=anthropic`` was *not* refused at startup as README section c
promises, and would have failed on the first model call instead — the exact failure that check
exists to prevent.

**Why both shapes are tested.** The validator runs before the field's type is applied, so it can be
handed a plain ``str`` (the environment: the case that was actually broken) or a ``SecretStr`` (a
caller constructing ``Settings`` directly). The first version of the fix handled only the string,
which looked complete and was not; the tests below therefore drive each shape through
``model_validate``, which is also the honest simulation of what the settings layer does with an
environment variable. The last test is the one that keeps a fix from becoming a different bug: a key
that is really there must still count, and must never be printable.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from agent_service.config import Settings

#: The two variables that arrive as empty strings from compose.
CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")


@pytest.fixture(autouse=True)
def _no_keys_from_this_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this machine's own keys out of the result.

    The tests below build settings from a mapping, which does not read the environment at all — so
    this fixture is not what makes them pass today. It is here so that if one of them is ever
    changed to construct ``Settings()`` the way the service does, the assertion still means what it
    says rather than depending on whoever ran it.
    """
    for name in CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)


def test_an_empty_key_from_the_environment_is_absent() -> None:
    """``KEY=`` and ``KEY=   `` both mean "not set" — the defect, in its original shape."""
    settings = Settings.model_validate({"anthropic_api_key": "", "openai_api_key": "   "})

    assert settings.anthropic_api_key is None
    assert settings.openai_api_key is None
    assert "anthropic" not in settings.configured_providers()
    assert "openai" not in settings.configured_providers()


def test_a_blank_secret_object_is_absent_as_well() -> None:
    """The other shape: a caller who passes a blank ``SecretStr`` must not have it counted.

    Nothing in the service does this today, which is exactly why it is worth pinning: it is the
    half of "blank" that a string-only check silently misses.
    """
    settings = Settings.model_validate({"anthropic_api_key": SecretStr("")})

    assert settings.anthropic_api_key is None
    assert "anthropic" not in settings.configured_providers()


def test_a_provider_with_a_blank_key_is_not_usable() -> None:
    """The startup refusal has to fire for a blank key, or it does not mean anything.

    ``agent_model_provider`` is a Literal, so this settings object can be built; the question is
    only whether the service notices that it has nothing to authenticate with.
    """
    settings = Settings.model_validate(
        {"agent_model_provider": "anthropic", "anthropic_api_key": ""}
    )

    assert settings.selected_provider_is_usable() is False


def test_a_real_key_is_usable_and_still_secret() -> None:
    """A key that is present still counts, and a ``repr()`` still shows nothing."""
    settings = Settings.model_validate(
        {"agent_model_provider": "anthropic", "anthropic_api_key": SecretStr("sk-ant-real-value")}
    )

    assert settings.selected_provider_is_usable() is True
    assert "anthropic" in settings.configured_providers()
    # The whole reason these fields are `SecretStr`: a log line, an exception or a trace span must
    # not be the place a key becomes readable.
    assert "sk-ant-real-value" not in repr(settings)
