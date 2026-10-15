"""The two endpoints the model picker talks to, and the header chip's source of truth.

Tested through `create_app` with injected handlers rather than through a live registry, because the
questions here are about the HTTP contract: which status code a refusal gets, that an unwired build
answers 501 instead of 500, that the chip follows the *live* selection rather than the startup
setting, and that no response can carry a credential.

The last one is a schema assertion rather than a value comparison. A test that sent a fake key and
checked it was absent would pass against a schema that had a field for keys and happened to be empty;
asserting the field set is what makes "there is nowhere for a key to go" the property under test.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi.testclient import TestClient

from agent_service.api.app import create_app
from agent_service.api.ui_routes import (
    ProviderSelectionError,
    ProviderStatus,
)
from agent_service.config import Settings

DETERMINISTIC_STATUS = ProviderStatus(
    provider="scripted",
    model_id=None,
    deterministic=True,
    available=["scripted", "replay", "openai"],
    models=[],
    detail="Answers from fixed rules in this repository.",
)

LIVE_STATUS = ProviderStatus(
    provider="openai",
    model_id="gpt-4o-mini",
    deterministic=False,
    available=["scripted", "replay", "openai"],
    models=["gpt-4o-mini", "gpt-4o"],
    detail="A model chooses the tools and writes the reply.",
)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"database_url": "sqlite+pysqlite:///:memory:"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _client(
    *,
    describe: Callable[[], Awaitable[ProviderStatus]] | None = None,
    choose: Callable[[str, str], Awaitable[ProviderStatus]] | None = None,
    settings: Settings | None = None,
) -> TestClient:
    app = create_app(
        settings or _settings(),
        describe_providers=describe,
        choose_provider=choose,
    )
    return TestClient(app)


async def _describe_live() -> ProviderStatus:
    return LIVE_STATUS


async def _choose_live(provider: str, model_id: str) -> ProviderStatus:
    return LIVE_STATUS.model_copy(update={"provider": provider, "model_id": model_id or None})


async def _choose_refuses(_provider: str, _model_id: str) -> ProviderStatus:
    raise ProviderSelectionError(
        "provider 'openai' is not usable in this process; available: scripted, replay"
    )


# --- GET /api/providers ------------------------------------------------------


def test_the_listing_reports_the_current_provider_with_the_alternatives() -> None:
    """All three answers at once: which is in force, which others could be, which models to offer."""
    client = _client(describe=_describe_live)

    body = client.get("/api/providers").json()

    assert body["provider"] == "openai"
    assert body["model_id"] == "gpt-4o-mini"
    assert body["deterministic"] is False
    assert body["available"] == ["scripted", "replay", "openai"]
    assert body["models"] == ["gpt-4o-mini", "gpt-4o"]
    assert body["detail"]


def test_an_unwired_listing_answers_501_rather_than_500() -> None:
    """"Not configured" stays distinguishable from "broken", as on the other endpoints."""
    response = _client().get("/api/providers")

    assert response.status_code == 501
    assert response.json()["error"]["kind"] == "NotImplemented"


# --- PUT /api/providers/current ---------------------------------------------


def test_selecting_a_provider_passes_it_through_and_reports_the_result() -> None:
    """The model id reaches the handler and comes back in the response the page redraws from."""
    seen: list[tuple[str, str]] = []

    async def choose(provider: str, model_id: str) -> ProviderStatus:
        seen.append((provider, model_id))
        return LIVE_STATUS

    client = _client(describe=_describe_live, choose=choose)

    response = client.put(
        "/api/providers/current", json={"provider": "openai", "model_id": "gpt-4o-mini"}
    )

    assert response.status_code == 200
    assert seen == [("openai", "gpt-4o-mini")]
    assert response.json()["provider"] == "openai"


def test_selecting_with_no_model_uses_the_providers_own_default() -> None:
    """Empty means "your default", the same convention the setting uses, so a caller can switch
    provider without knowing a single model id."""
    client = _client(choose=_choose_live)

    body = client.put("/api/providers/current", json={"provider": "scripted"}).json()

    assert body["provider"] == "scripted"
    assert body["model_id"] is None


def test_a_refused_selection_is_a_400_with_the_reason() -> None:
    """A 500 here would tell the reader the service is broken; the refusal is something they can
    fix, and the message names what is available instead."""
    client = _client(choose=_choose_refuses)

    response = client.put("/api/providers/current", json={"provider": "openai"})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["kind"] == "ProviderNotConfigured"
    assert "scripted" in error["detail"]


def test_an_unwired_selection_answers_501() -> None:
    response = _client().put("/api/providers/current", json={"provider": "scripted"})

    assert response.status_code == 501
    assert response.json()["error"]["kind"] == "NotImplemented"


def test_a_malformed_selection_body_is_refused_before_the_handler_runs() -> None:
    """No provider field at all is a client bug, and FastAPI rejects it as one."""
    called = False

    async def choose(_provider: str, _model_id: str) -> ProviderStatus:
        nonlocal called
        called = True
        return LIVE_STATUS

    response = _client(choose=choose).put("/api/providers/current", json={"model_id": "x"})

    assert response.status_code == 422
    assert called is False


# --- GET /api/info ----------------------------------------------------------


def test_the_header_follows_the_live_selection_not_the_startup_setting() -> None:
    """The defect this ordering fixes, stated as a test.

    `/api/info` used to read `app.state.settings`, so after a provider was changed on the page the
    chip kept naming the one the process *started* with — a header that contradicts the agent is
    worse than no header.
    """
    client = _client(
        describe=_describe_live,
        settings=_settings(agent_model_provider="scripted"),
    )

    body = client.get("/api/info").json()

    assert body["provider"] == "openai"
    assert body["model_id"] == "gpt-4o-mini"
    assert body["deterministic"] is False


def test_without_a_control_the_header_still_answers_from_settings() -> None:
    """A build with no registry must render a page and a chip rather than a 500."""
    client = _client(settings=_settings(agent_model_provider="scripted"))

    body = client.get("/api/info").json()

    assert body["provider"] == "scripted"
    assert body["deterministic"] is True
    assert "scripted" in body["configured"]
    assert "no language model" in body["detail"].lower()


def test_a_configured_live_provider_is_described_as_live() -> None:
    client = _client(
        settings=_settings(agent_model_provider="openai", openai_api_key="sk-test-not-a-real-key")
    )

    body = client.get("/api/info").json()

    assert body["deterministic"] is False
    assert "openai" in body["configured"]
    # The credential's presence is reported; the credential is not.
    assert "sk-test-not-a-real-key" not in body["detail"]


# --- the shape itself -------------------------------------------------------


def test_the_status_shape_has_nowhere_to_put_a_credential() -> None:
    """Asserted rather than assumed, because the picker is the one endpoint a key could reach.

    A field for a key would be filled by whatever the wiring layer had to hand; the absence of the
    field is what makes that impossible rather than merely unlikely.
    """
    fields = set(ProviderStatus.model_fields)

    assert fields == {"provider", "model_id", "deterministic", "available", "models", "detail"}
    assert not any("key" in name or "token" in name or "secret" in name for name in fields)
