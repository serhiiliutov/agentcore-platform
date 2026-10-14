"""Tests for the provider layer: the deterministic providers, the factory, and hygiene.

Hermetic. No key, no network, no SDK call. The SDK-backed providers are exercised only where
they can be checked without one — that a missing credential is refused at construction, and
that the neutral types cannot carry a secret.

The hygiene tests are the ones ADR-0002 asks for by name: "a test asserts each of those,
rather than trusting the intent". They are here rather than in the web-UI stage because the
neutral types are what everything downstream serialises, so this is the cheapest place to
prove that a key cannot ride along in a trace.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_service.config import ProviderName, Settings
from agent_service.providers import (
    AssistantTurn,
    Message,
    ModelProvider,
    ProviderNotConfiguredError,
    ToolCallRequest,
    ToolSchema,
    Usage,
    build_provider,
)
from agent_service.providers.replay import RecordingExhaustedError, ReplayProvider
from agent_service.providers.scripted import (
    CREATE_PHRASES,
    DEFAULT_RULES,
    SLOW_TURN_DELAY_S,
    SLOW_TURN_PHRASE,
    Rule,
    ScriptedProvider,
)

#: A valid SQLAlchemy URL; nothing here opens a database, but Settings validates the scheme.
_TEST_DATABASE_URL = "sqlite+pysqlite:///:memory:"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"database_url": _TEST_DATABASE_URL}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _user(text: str) -> list[Message]:
    return [Message.system("be helpful"), Message.user(text)]


# --- scripted: determinism ----------------------------------------------------


async def test_the_same_conversation_produces_the_same_turn_twice() -> None:
    """The property that makes an eval meaningful: a failure can be re-run to the failure."""
    provider = ScriptedProvider()

    first = await provider.chat(_user("please open a ticket about the printer"))
    second = await provider.chat(_user("please open a ticket about the printer"))

    assert first == second


async def test_scripted_satisfies_the_provider_protocol() -> None:
    assert isinstance(ScriptedProvider(), ModelProvider)


async def test_scripted_reports_its_own_name() -> None:
    assert ScriptedProvider().name == "scripted"


# --- scripted: the paths an eval needs ---------------------------------------


async def test_a_search_request_becomes_a_search_call() -> None:
    turn = await ScriptedProvider().chat(_user("find tickets about the printer"))

    assert turn.tool_calls
    assert turn.tool_calls[0].name == "search_tickets"
    assert turn.tool_calls[0].arguments["q"] == "printer"


async def test_a_named_ticket_becomes_a_lookup() -> None:
    turn = await ScriptedProvider().chat(_user("what is the status of TCK-1001?"))

    assert turn.tool_calls[0].name == "get_ticket"
    assert turn.tool_calls[0].arguments["ticket_id"] == "TCK-1001"


async def test_an_explicit_ticket_wins_over_a_search_word() -> None:
    """Ordering is the decision procedure, so it is worth pinning.

    The prompt contains both "find" and a ticket id; the id rule is earlier, so the lookup
    wins. Reordering DEFAULT_RULES would silently change this.
    """
    turn = await ScriptedProvider().chat(_user("find me TCK-1001"))

    assert turn.tool_calls[0].name == "get_ticket"


async def test_a_ticket_request_becomes_a_write_with_a_stable_key() -> None:
    """The write path, and the fixed key that makes a re-run provably the same action.

    This is what an eval drives to reach the approval gate, so the provider must be able to
    propose a write at all.
    """
    turn = await ScriptedProvider().chat(_user("open a ticket about the printer"))

    call = turn.tool_calls[0]
    assert call.name == "create_ticket"
    assert call.arguments["client_request_id"] == "scripted-create-1"


async def test_a_ticket_title_is_within_the_servers_bounds() -> None:
    """The server rejects a title under three characters, so the rule must not produce one."""
    turn = await ScriptedProvider().chat(_user("open a ticket"))
    title = str(turn.tool_calls[0].arguments["title"])

    assert 3 <= len(title) <= 120


async def test_the_create_phrase_is_removed_from_the_title() -> None:
    """Otherwise every scripted ticket is titled after the command that made it."""
    turn = await ScriptedProvider().chat(_user("open a ticket about the printer"))
    title = str(turn.tool_calls[0].arguments["title"])

    assert not any(phrase in title for phrase in CREATE_PHRASES)
    assert "printer" in title


async def test_a_tool_result_ends_the_conversation_instead_of_calling_again() -> None:
    """The terminating rule.

    Without it a scripted run would call tools until the step ceiling, so every eval would end
    in max_steps and prove nothing about the loop.
    """
    messages = [
        Message.system("be helpful"),
        Message.user("find tickets about the printer"),
        Message.assistant(
            "searching", [ToolCallRequest(id="c1", name="search_tickets", arguments={"q": "x"})]
        ),
        Message.tool_result(ToolCallRequest(id="c1", name="search_tickets"), "3 of 3 found"),
    ]

    turn = await ScriptedProvider().chat(messages)

    assert turn.tool_calls == ()
    assert turn.stop_reason == "end_turn"


async def test_an_error_result_is_reported_as_a_refusal_to_retry() -> None:
    """A scripted run must not quietly retry a failed tool: it says it is not going to."""
    call = ToolCallRequest(id="c1", name="search_tickets")
    messages = [
        Message.user("find tickets"),
        Message.assistant("searching", [call]),
        Message.tool_result(call, "ERROR: the backend is down"),
    ]

    turn = await ScriptedProvider().chat(messages)

    assert "not going to keep retrying" in turn.text


async def test_an_unmatched_prompt_produces_a_plain_answer() -> None:
    turn = await ScriptedProvider().chat(_user("hello there"))

    assert turn.tool_calls == ()
    assert "hello there" in turn.text


async def test_a_rule_naming_an_unavailable_tool_falls_through() -> None:
    """A provider must not emit a call the server cannot serve.

    The loop refuses an unknown tool name, so a rule that produced one would fail at the call
    rather than at the decision — a worse place to find out.
    """
    turn = await ScriptedProvider().chat(
        _user("find tickets about the printer"),
        tools=[ToolSchema(name="something_else", description="", input_schema={})],
    )

    assert turn.tool_calls == (), "the search rule must not fire when search is not offered"


def test_the_default_program_ends_with_a_terminating_rule() -> None:
    """Pinned because the last rule is the loop's only guaranteed exit under a script."""
    assert DEFAULT_RULES[-1].name == "answer_after_tools"


# --- scripted: the one rule that is about how long it takes -------------------


def test_the_slow_rule_is_first_in_the_program() -> None:
    """Order is the decision procedure, and this rule is first for a reason.

    It matches a prompt that asked to be slow. Every other rule would answer that prompt before
    the delay could happen, so a later position would make the eval's deadline case end as a plain
    answer — passing while measuring nothing about the budget.
    """
    assert DEFAULT_RULES[0].name == "slow_turn"
    assert DEFAULT_RULES[0].delay_s == SLOW_TURN_DELAY_S


def test_the_slow_rule_matches_the_phrase_the_eval_case_uses() -> None:
    """The phrase is a constant shared with the case file, so the two cannot drift apart.

    If they did, the case would take a normal-speed turn, meet no deadline, and fail with a
    message about the wrong thing entirely.
    """
    provider = ScriptedProvider(rules=(Rule("slow_turn", DEFAULT_RULES[0].matches, _plain_turn),))

    assert not provider.rules[0].matches(_user("find tickets about the printer"))
    assert provider.rules[0].matches(_user(f"find the printer, and {SLOW_TURN_PHRASE} allows"))


def test_the_slow_turn_still_does_the_work() -> None:
    """It searches after waiting, rather than idling.

    A turn that was cut short while doing nothing would say nothing about whether the budget
    bounds a call. Called on the rule directly so the test does not pay the whole two seconds.
    """
    turn = DEFAULT_RULES[0].emit(_user(f"find tickets about the printer, and {SLOW_TURN_PHRASE}"))

    assert turn.tool_calls[0].name == "search_tickets"
    assert turn.tool_calls[0].arguments["q"] == "printer"


def _plain_turn(_messages: Sequence[Message]) -> AssistantTurn:
    return AssistantTurn(text="ok", stop_reason="end_turn")


async def test_a_rule_delay_is_honoured_and_absent_by_default() -> None:
    """The mechanism the deadline case rests on, measured at both ends.

    Measured rather than argued: a delayed rule must take its delay, and an ordinary one must not
    take anything — a provider that slept on every call would slow the whole suite until somebody
    deleted the timeout case, which is exactly how a coverage claim quietly becomes false.
    """
    slow = ScriptedProvider(rules=(Rule("slow", _matches_always, _plain_turn, delay_s=0.2),))
    quick = ScriptedProvider(rules=(Rule("quick", _matches_always, _plain_turn),))

    started = time.monotonic()
    await slow.chat(_user("anything"))
    delayed = time.monotonic() - started

    started = time.monotonic()
    await quick.chat(_user("anything"))
    immediate = time.monotonic() - started

    assert delayed >= 0.2, "a rule with a delay must take it"
    assert immediate < 0.2, "a rule without one must not"


def _matches_always(_messages: Sequence[Message]) -> bool:
    return True


async def test_a_delay_is_not_repeated_once_a_tool_has_run() -> None:
    """The slow rule is guarded like the others, so a second model turn does not wait again.

    Without the guard the delay would apply to every turn that mentioned the phrase, including
    the one reporting the result — which would double the cost of the case and make the deadline
    arrive at a different moment than anyone expects.
    """
    call = ToolCallRequest(id="c1", name="search_tickets", arguments={"q": "printer"})
    messages = [
        Message.user(f"find tickets about the printer, and {SLOW_TURN_PHRASE}"),
        Message.assistant("searching", [call]),
        Message.tool_result(call, "3 of 3 found"),
    ]

    assert not DEFAULT_RULES[0].matches(messages)


# --- replay -------------------------------------------------------------------


def _write_recording(path: Path, turns: list[dict[str, object]]) -> None:
    path.write_text(json.dumps(turns), encoding="utf-8")


def test_a_recording_is_loaded_in_order(tmp_path: Path) -> None:
    path = tmp_path / "recording.json"
    _write_recording(
        path,
        [
            {"text": "first"},
            {
                "text": "second",
                "stop_reason": "tool_use",
                "tool_calls": [{"id": "c1", "name": "t"}],
            },
        ],
    )

    provider = ReplayProvider.from_settings(
        _settings(agent_model_provider="replay", agent_model_id=str(path))
    )

    assert [turn.text for turn in provider.turns] == ["first", "second"]
    assert provider.turns[1].tool_calls[0].name == "t"
    assert provider.turns[1].stop_reason == "tool_use"


async def test_replay_yields_turns_in_order(tmp_path: Path) -> None:
    path = tmp_path / "recording.json"
    _write_recording(path, [{"text": "first"}, {"text": "second"}])
    provider = ReplayProvider.from_settings(
        _settings(agent_model_provider="replay", agent_model_id=str(path))
    )

    assert (await provider.chat([])).text == "first"
    assert (await provider.chat([])).text == "second"


async def test_exhausting_a_recording_raises_rather_than_restarting(tmp_path: Path) -> None:
    """A replay that wrapped around would make a non-terminating loop look terminating."""
    path = tmp_path / "recording.json"
    _write_recording(path, [{"text": "only"}])
    provider = ReplayProvider.from_settings(
        _settings(agent_model_provider="replay", agent_model_id=str(path))
    )

    await provider.chat([])

    with pytest.raises(RecordingExhaustedError):
        await provider.chat([])


def test_a_recording_with_no_path_is_refused() -> None:
    with pytest.raises(ProviderNotConfiguredError):
        ReplayProvider.from_settings(_settings(agent_model_provider="replay"))


def test_a_missing_recording_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ProviderNotConfiguredError):
        ReplayProvider.from_settings(
            _settings(agent_model_provider="replay", agent_model_id=str(tmp_path / "absent.json"))
        )


def test_a_recording_that_is_not_an_array_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "recording.json"
    path.write_text('{"text": "not an array"}', encoding="utf-8")

    with pytest.raises(ProviderNotConfiguredError):
        ReplayProvider.from_settings(
            _settings(agent_model_provider="replay", agent_model_id=str(path))
        )


def test_a_recording_that_is_not_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "recording.json"
    path.write_text("definitely not json", encoding="utf-8")

    with pytest.raises(ProviderNotConfiguredError):
        ReplayProvider.from_settings(
            _settings(agent_model_provider="replay", agent_model_id=str(path))
        )


def test_replay_accepts_a_directory_and_looks_for_the_default_name(tmp_path: Path) -> None:
    """An operator setting a run directory should not have to know the file name."""
    from agent_service.providers.replay import DEFAULT_FILENAME

    _write_recording(tmp_path / DEFAULT_FILENAME, [{"text": "hi"}])

    provider = ReplayProvider.from_settings(
        _settings(agent_model_provider="replay", agent_model_id=str(tmp_path))
    )

    assert provider.turns[0].text == "hi"


# --- the factory --------------------------------------------------------------


def test_the_default_provider_is_scripted_and_needs_no_credential() -> None:
    """The property that lets a bare checkout work with nothing set."""
    provider = build_provider(_settings())

    assert isinstance(provider, ScriptedProvider)


def test_an_unconfigured_provider_is_refused_before_it_is_used() -> None:
    """'Not configured' must arrive at startup, not as an auth error mid-conversation."""
    with pytest.raises(ProviderNotConfiguredError) as raised:
        build_provider(_settings(agent_model_provider="anthropic"))

    assert "not configured" in str(raised.value)


def test_the_refusal_names_what_is_available() -> None:
    """An operator reading the message should learn what they can use instead."""
    with pytest.raises(ProviderNotConfiguredError) as raised:
        build_provider(_settings(agent_model_provider="anthropic"))

    assert "scripted" in str(raised.value)


# --- secret hygiene -----------------------------------------------------------


#: A value shaped like a real credential, used to prove it cannot travel through the neutral
#: types. Not a real key, and deliberately recognisable so a leak is unmistakable.
CANARY_SECRET = "sk-CANARY-must-never-appear"


def test_the_neutral_types_cannot_carry_a_secret() -> None:
    """The structural reason a key cannot reach a trace.

    These types have no credential field, so there is nowhere for a key to be attached and
    therefore nothing for a serialiser to emit. This is the check ADR-0002 asks for: the
    absence is asserted rather than assumed.
    """
    fields = set(Message.__dataclass_fields__) | set(ToolCallRequest.__dataclass_fields__)
    fields |= set(AssistantTurn.__dataclass_fields__) | set(Usage.__dataclass_fields__)
    fields |= set(ToolSchema.__dataclass_fields__)

    for suspicious in ("key", "api_key", "token", "secret", "password", "credential"):
        assert suspicious not in fields


def test_a_settings_repr_does_not_contain_the_key() -> None:
    """SecretStr, so a settings repr in a log line shows asterisks."""
    settings = _settings(anthropic_api_key=CANARY_SECRET)

    assert CANARY_SECRET not in repr(settings)
    assert CANARY_SECRET not in str(settings)


def test_configured_providers_leaks_no_key_material() -> None:
    """What the web UI is allowed to know.

    The provider list is the one place a key's presence is observable, so it must report
    *whether* a provider is usable and nothing about the credential itself.
    """
    settings = _settings(anthropic_api_key=CANARY_SECRET)

    reported = settings.configured_providers()

    assert "anthropic" in reported
    assert CANARY_SECRET not in repr(reported)
    assert CANARY_SECRET not in "".join(reported)


def test_a_secret_is_not_readable_without_asking_explicitly() -> None:
    """The mechanism behind the two tests above: the value is not a str."""
    settings = _settings(anthropic_api_key=CANARY_SECRET)

    assert settings.anthropic_api_key is not None
    assert not isinstance(settings.anthropic_api_key, str)


@pytest.mark.parametrize("provider", ["scripted", "replay"])
def test_the_credential_free_providers_are_always_reported_as_configured(
    provider: ProviderName,
) -> None:
    """They need nothing, so a deployment can always fall back to one."""
    assert provider in _settings().configured_providers()
