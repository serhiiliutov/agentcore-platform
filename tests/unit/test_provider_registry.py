"""Changing the model provider while the service runs, and remembering the choice.

Two subjects, and they are together because they are one feature: `ProviderRegistry` swaps the
provider in memory, `ProviderControl` stores the swap and reads it back. Testing them apart would
leave the property that matters — a choice survives a restart of the process — asserted nowhere.

The first test is the one that would have caught the defect this module was written for. Every
provider class in `agent_service.providers` was complete, with its SDK declared and installed, and
`build_provider` never dispatched to any of them: selecting `openai` raised "its implementation is
not written yet". So a test that selects one and *gets a provider back* is the regression guard, and
it is why the assertion is on the built instance rather than on the name alone.

Hermetic. No model call is made anywhere here: constructing a provider only reads settings, and the
one path that would reach the network — listing OpenAI's models — is stubbed in the test that
exercises its fallback rather than its success. The credentials used are shaped like keys and are
never sent.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import agent_service.db.models  # noqa: F401 - registers every table on Base.metadata
from agent_service.api.client_routes import ProviderSelectionError
from agent_service.api.provider_control import KNOWN_PROVIDERS, ProviderControl
from agent_service.config import REPLAY_DEFAULT_FILENAME, Settings
from agent_service.db.base import Base, create_session_factory, register_sqlite_pragmas
from agent_service.db.repositories.provider_settings import (
    GLOBAL_SCOPE,
    ProviderSettingsRepository,
)
from agent_service.providers.base import ProviderNotConfiguredError
from agent_service.providers.registry import (
    ProviderRegistry,
    ProviderSelection,
    default_model_for,
    is_deterministic,
)

#: Shaped like a key, not a key. Nothing here is sent anywhere: none of these tests make a call.
FAKE_OPENAI_KEY = "sk-test-not-a-real-key-0000"


#: Every field `Settings.configured_providers` consults, set explicitly to nothing.
#:
#: Named rather than left to the environment, and that is the difference between a hermetic test and
#: one that passes on a machine without a key and fails on a machine with one. `Settings` reads the
#: ambient environment for any field it is not given, so a developer's `.env` — which is *expected*
#: to hold a real `OPENAI_API_KEY` the moment they try a live model — silently made `openai`
#: buildable and turned the four refusal tests below red for a reason that had nothing to do with
#: the code. It did exactly that, which is how this list came to be written.
#:
#: The list is the whole of `configured_providers`, so "no credentials" below means precisely what
#: it says: no provider beyond the deterministic pair can be built from these settings.
NO_CREDENTIALS: dict[str, object] = {
    "anthropic_api_key": None,
    "openai_api_key": None,
    "openai_base_url": "",
    "aws_region": "",
}


def _settings(**overrides: object) -> Settings:
    """Settings with a usable database URL and **no credentials at all**.

    `scripted` is then the only provider that can be built, which is both the state a fresh checkout
    is in and the state the refusal tests require. `overrides` is applied last, so a test that wants
    a key passes one and gets a provider that is genuinely buildable — see `NO_CREDENTIALS` for why
    the absent ones are named rather than inherited.
    """
    base: dict[str, object] = {"database_url": "sqlite+pysqlite:///:memory:"}
    base.update(NO_CREDENTIALS)
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite with the pragmas production uses, so the schema behaves the same."""
    built = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    register_sqlite_pragmas(built)
    Base.metadata.create_all(built)
    yield built
    built.dispose()


@pytest.fixture
def sessions(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


# --- building: the dispatch that was missing ---------------------------------


def test_the_registry_starts_from_the_setting() -> None:
    """The environment is still the initial value; nothing here replaces it."""
    registry = ProviderRegistry(_settings())

    assert registry.current.provider == "scripted"
    assert registry.provider.name == "scripted"


async def test_selecting_openai_builds_a_real_provider() -> None:
    """The regression guard for the whole feature.

    `openai` was rejected with "its implementation is not written yet" while `OpenAIProvider` sat in
    the same package with `openai>=1.84` declared and installed. Constructing it needs no network
    call, so this asserts on the instance rather than on the name — an implementation that reported
    "openai" without being one would pass a name check.
    """
    from agent_service.providers.openai_provider import OpenAIProvider

    registry = ProviderRegistry(_settings(openai_api_key=FAKE_OPENAI_KEY))

    selection = await registry.select("openai", "gpt-4o-mini")

    assert selection.provider == "openai"
    assert selection.model_id == "gpt-4o-mini"
    assert isinstance(registry.provider, OpenAIProvider)
    assert registry.provider.name == "openai"


async def test_selecting_anthropic_builds_a_real_provider() -> None:
    from agent_service.providers.anthropic_provider import AnthropicProvider

    registry = ProviderRegistry(_settings(anthropic_api_key="sk-ant-not-a-real-key"))

    await registry.select("anthropic")

    assert isinstance(registry.provider, AnthropicProvider)


def test_a_local_endpoint_needs_no_key_and_reports_itself_as_compatible() -> None:
    """A base URL is all a local endpoint needs, and the provider says what it really is.

    Two facts, and the first version of this test conflated them: a base URL does not *choose* the
    provider — `AGENT_MODEL_PROVIDER` still does — it only makes `openai_compatible` buildable
    without a credential. What the class then reports is its own name, taken from the base URL, so
    the trace says `openai_compatible` for a server that is not OpenAI and `openai` for one that is.
    """
    registry = ProviderRegistry(
        _settings(
            agent_model_provider="openai_compatible",
            openai_base_url="http://localhost:11434/v1",
        )
    )

    assert registry.current.provider == "openai_compatible"
    assert registry.provider.name == "openai_compatible"


# --- refusing: the properties that keep a bad choice harmless ----------------


async def test_a_provider_with_no_credential_is_refused() -> None:
    registry = ProviderRegistry(_settings())

    with pytest.raises(ProviderNotConfiguredError) as raised:
        await registry.select("openai")

    assert "openai" in str(raised.value)


async def test_a_refused_selection_leaves_the_running_provider_alone() -> None:
    """The property that makes a provider list safe to show: a wrong click cannot break a turn.

    The candidate is built before anything is adopted, so the refusal happens while the working
    provider is still installed.
    """
    registry = ProviderRegistry(_settings())

    with pytest.raises(ProviderNotConfiguredError):
        await registry.select("anthropic", "claude-sonnet-4-5")

    assert registry.current.provider == "scripted"
    assert registry.provider.name == "scripted"


async def test_an_unknown_provider_name_is_refused() -> None:
    """The name is validated here rather than by the type, because a request body is a string.

    `select` takes `str` on purpose: the caller is usually JSON. This is the test that the validation
    actually happens at that boundary, rather than the name being assumed correct because the
    parameter was annotated.
    """
    registry = ProviderRegistry(_settings())

    with pytest.raises(ProviderNotConfiguredError):
        await registry.select("gpt-5")


def test_the_availability_list_is_the_settings_answer() -> None:
    """One source for "what could run here", so the page and the startup check cannot disagree."""
    assert ProviderRegistry(_settings()).available() == ("scripted",)

    with_key = ProviderRegistry(_settings(openai_api_key=FAKE_OPENAI_KEY))
    assert "openai" in with_key.available()
    assert with_key.usable("openai") is True
    assert with_key.usable("anthropic") is False


def test_replay_is_offered_only_when_a_recording_is_actually_there(tmp_path: Path) -> None:
    """`replay` is not the same kind of provider as the others, and the picker has to know it.

    Every other provider is buildable from a credential in the environment; `replay` is buildable
    from a *file*, whose path travels in `AGENT_MODEL_ID` — the field the others use for a model
    name. Listing it unconditionally therefore offered a choice that could only fail, and that is
    what a reader hit: the dropdown listed `replay`, selecting it sent a model id where a path
    belongs, and the answer was "recording not found: gpt-4o-mini".

    Both directions are asserted, because "absent with no file" is only half the property. A gate
    that also hid `replay` from a correctly configured deployment would break the provider the
    recorded-run path exists for — and this is the test that would notice.
    """
    assert "replay" not in ProviderRegistry(_settings(agent_model_id="")).available()
    assert (
        "replay" not in ProviderRegistry(_settings(agent_model_id=str(tmp_path))).available()
    ), "a directory with no recording inside it is as absent as an empty setting"

    # Named by its directory rather than its path: the provider resolves one to the other, and the
    # gate has to agree with it or a directory setting would be offered and then rejected on use.
    (tmp_path / REPLAY_DEFAULT_FILENAME).write_text("[]", encoding="utf-8")

    assert "replay" in ProviderRegistry(_settings(agent_model_id=str(tmp_path))).available()
    assert (
        "replay"
        in ProviderRegistry(
            _settings(agent_model_id=str(tmp_path / REPLAY_DEFAULT_FILENAME))
        ).available()
    )


# --- the deterministic set ----------------------------------------------------


def test_the_deterministic_pair_is_recognised_and_nothing_else_is() -> None:
    """The page shows this to a reader, so it must be a statement about the providers, not a guess."""
    assert is_deterministic("scripted") is True
    assert is_deterministic("replay") is True
    assert is_deterministic("openai") is False
    assert ProviderSelection(provider="scripted").deterministic is True


def test_each_provider_offers_the_model_it_would_use_by_default() -> None:
    """Read from the provider modules, so the suggestion cannot drift from the behaviour."""
    assert default_model_for("openai") == "gpt-4o"
    assert default_model_for("anthropic") == "claude-sonnet-4-5"
    assert default_model_for("bedrock").startswith("anthropic.")
    # Neither answers from a model, so neither has a model id to suggest.
    assert default_model_for("scripted") == ""
    assert default_model_for("replay") == ""


# --- listing models ----------------------------------------------------------


async def test_a_model_listing_that_fails_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listing fills a suggestion list, so a failure must not become an error in the page.

    Stubbed rather than skipped: this is the path taken by a wrong key, an unreachable endpoint or
    an endpoint with no listing route, and it is the path a reviewer is most likely to hit.
    """

    async def explode() -> list[str]:
        raise RuntimeError("no catalogue here")

    registry = ProviderRegistry(_settings(openai_api_key=FAKE_OPENAI_KEY))
    monkeypatch.setattr(registry, "_openai_model_ids", explode)

    assert await registry.list_models("openai") == ("gpt-4o",)


async def test_a_provider_without_a_catalogue_offers_its_default() -> None:
    registry = ProviderRegistry(_settings(anthropic_api_key="sk-ant-not-a-real-key"))

    assert await registry.list_models("anthropic") == ("claude-sonnet-4-5",)


# --- adopting a stored choice -------------------------------------------------


def test_adopting_a_stored_selection_uses_it() -> None:
    registry = ProviderRegistry(_settings(openai_api_key=FAKE_OPENAI_KEY))

    adopted = registry.adopt(ProviderSelection(provider="openai", model_id="gpt-4o-mini"))

    assert adopted is True
    assert registry.current.provider == "openai"
    assert registry.current.model_id == "gpt-4o-mini"


def test_a_stored_selection_that_can_no_longer_run_is_not_adopted() -> None:
    """A preference must never be able to stop the service from starting.

    This is the case where somebody picks OpenAI on the page, the key is later removed from the
    environment, and the container restarts: the service comes up on the configured provider and
    says so in the log, rather than refusing to serve.
    """
    registry = ProviderRegistry(_settings())  # no key in this process

    adopted = registry.adopt(ProviderSelection(provider="openai", model_id="gpt-4o-mini"))

    assert adopted is False
    assert registry.current.provider == "scripted"


# --- the control: storing the choice -----------------------------------------


async def test_choosing_stores_the_selection(sessions: sessionmaker[Session]) -> None:
    registry = ProviderRegistry(_settings(openai_api_key=FAKE_OPENAI_KEY))
    control = ProviderControl(registry=registry, sessions=sessions)

    status = await control.choose("openai", "gpt-4o-mini")

    assert status.provider == "openai"
    assert status.model_id == "gpt-4o-mini"
    assert status.deterministic is False

    with sessions() as session:
        row = ProviderSettingsRepository(session).current()
    assert row is not None
    assert row.scope == GLOBAL_SCOPE
    assert (row.provider, row.model_id) == ("openai", "gpt-4o-mini")


async def test_a_stored_selection_is_read_back_by_a_later_process(
    sessions: sessionmaker[Session],
) -> None:
    """The whole point of storing it: a new registry, same database, same provider."""
    settings = _settings(openai_api_key=FAKE_OPENAI_KEY)
    await ProviderControl(
        registry=ProviderRegistry(settings), sessions=sessions
    ).choose("openai", "gpt-4o-mini")

    restarted = ProviderRegistry(settings)
    control = ProviderControl(registry=restarted, sessions=sessions)

    assert control.restore() is True
    assert restarted.current.provider == "openai"
    assert restarted.current.model_id == "gpt-4o-mini"


def test_restoring_with_nothing_stored_reports_no_change(
    sessions: sessionmaker[Session],
) -> None:
    """A fresh installation has no row, and that is not a failure — it follows the environment."""
    registry = ProviderRegistry(_settings())
    control = ProviderControl(registry=registry, sessions=sessions)

    assert control.restore() is False
    assert registry.current.provider == "scripted"


async def test_a_choice_is_refused_with_a_reason_and_nothing_is_stored(
    sessions: sessionmaker[Session],
) -> None:
    """A refusal must not write a row: the database must not describe a provider that is not in use."""
    registry = ProviderRegistry(_settings())
    control = ProviderControl(registry=registry, sessions=sessions)

    with pytest.raises(ProviderSelectionError):
        await control.choose("openai", "gpt-4o-mini")

    with sessions() as session:
        assert ProviderSettingsRepository(session).current() is None
    assert registry.current.provider == "scripted"


async def test_an_unknown_provider_is_refused_by_name(
    sessions: sessionmaker[Session],
) -> None:
    """The message tells the caller what this service does offer, rather than only what it does not."""
    control = ProviderControl(registry=ProviderRegistry(_settings()), sessions=sessions)

    with pytest.raises(ProviderSelectionError) as raised:
        await control.choose("langchain", "")

    assert "langchain" in str(raised.value)
    assert "scripted" in str(raised.value)
    assert set(KNOWN_PROVIDERS) >= {"scripted", "openai", "anthropic", "bedrock"}


async def test_describing_reports_what_is_available_and_never_a_credential(
    sessions: sessionmaker[Session],
) -> None:
    """The picker's whole input, and the one thing it must not contain."""
    registry = ProviderRegistry(_settings(openai_api_key=FAKE_OPENAI_KEY))
    control = ProviderControl(registry=registry, sessions=sessions)

    status = await control.describe()

    assert status.provider == "scripted"
    assert status.deterministic is True
    assert "openai" in status.available
    assert FAKE_OPENAI_KEY not in status.model_dump_json()
