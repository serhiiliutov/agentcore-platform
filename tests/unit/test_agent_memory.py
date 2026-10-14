"""The agent-side half of memory: what is extracted, what is injected, and who sees it.

The repository tests in `test_memory.py` prove the store works. These prove it is *used* — which
is a different claim, and the one that was false. `FactRepository` had no caller in the request
path: nothing extracted a fact from a conversation and nothing put one back into a prompt, so the
assignment's "at least one cross-session fact" was satisfied by a table and a repository rather
than by the agent. The eval suite's `memory_*` cases found it; this file is what pins the fix so
it cannot be removed silently.

Three claims, one per group below:

1. Extraction reads the user's words and only the whitelisted keys.
2. Rendering produces a block only when there is something to say.
3. A turn's facts reach the next turn's prompt — for that user and no other.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool

import agent_service.db.models  # noqa: F401 - registers every table on Base.metadata
from agent_service.agent import memory
from agent_service.agent.handler import InvocationHandlerFactory
from agent_service.api.models import InvocationRequest
from agent_service.config import ProviderName
from agent_service.db.base import Base, create_session_factory, register_sqlite_pragmas
from agent_service.mcp.client import McpTool
from agent_service.providers.base import AssistantTurn, Message, ToolSchema
from agent_service.providers.scripted import ScriptedProvider
from agent_service.tools.registry import ToolRegistry

USER = "memory-user"
OTHER_USER = "memory-other"
SESSION_A = "memory-session-a"
SESSION_B = "memory-session-b"


# --- 1. extraction ------------------------------------------------------------


def test_a_stated_name_is_extracted() -> None:
    assert memory.extract("Hi, I'm Priya Raman.") == {"requester_name": "Priya Raman"}


def test_a_stated_team_is_extracted_without_becoming_a_name() -> None:
    """The misreading the stopwords exist for.

    "I'm on the B team" matches the name pattern too, and a service that stored a person's name
    as "On The" would be confidently wrong about them in every later conversation.
    """
    found = memory.extract("I'm on the B team")

    assert found == {"team": "B"}
    assert "requester_name" not in found


def test_a_stated_preference_is_extracted() -> None:
    assert memory.extract("I prefer hardware") == {"preferred_category": "hardware"}


def test_an_unrelated_prompt_states_nothing() -> None:
    """The common case, and the one worth asserting: most prompts contain no facts at all."""
    assert memory.extract("find tickets about the printer") == {}


def test_a_sentence_is_not_stored_as_a_name() -> None:
    """Whitelist shape, not just whitelist keys.

    The extractor is bounded to two words, so a prompt that begins "I'm looking into the printer
    for the third time this week" cannot deposit that sentence in a field the UI renders as a
    person's name.
    """
    found = memory.extract("I'm looking into the printer for the third time this week")

    assert "requester_name" not in found


def test_extraction_never_returns_a_key_outside_the_whitelist() -> None:
    """Whatever the prompt says, only the three known keys can come back.

    This is the property that keeps memory inspectable and keeps a ticket body from becoming
    durable state about a person.
    """
    wordy = (
        "I'm Dana. I'm on the security team. I prefer network. "
        "My password is hunter2 and my manager is Priya."
    )

    assert set(memory.extract(wordy)) <= set(memory.FACT_KEYS)


# --- 2. rendering -------------------------------------------------------------


def test_nothing_to_say_produces_no_block() -> None:
    """Not a heading with nothing under it: a model told to expect facts will sometimes supply
    them, and an empty heading is an invitation to do exactly that."""
    assert memory.render({}) == ""
    assert memory.with_facts("base", {}) == "base"


def test_facts_render_under_the_heading_in_a_stable_order() -> None:
    block = memory.render({"team": "B", "requester_name": "Dana"})

    assert block.startswith(memory.FACTS_HEADING)
    lines = block.splitlines()[1:]
    assert lines == ["- requester_name: Dana", "- team: B"], "order follows FACT_KEYS"


def test_an_unknown_key_is_not_rendered() -> None:
    """A row written by hand, or by an older version, must not reach a prompt unvetted."""
    assert memory.render({"requester_name": "Dana", "favourite_colour": "green"}) == (
        f"{memory.FACTS_HEADING}\n- requester_name: Dana"
    )


# --- 3. the wiring ------------------------------------------------------------


@dataclass
class RecordingProvider:
    """Records every message list it is shown, and answers plainly."""

    seen: list[list[Message]] = field(default_factory=list)
    #: Typed as the provider-name literal rather than `str`, because `ModelProvider.name` is that
    #: literal. A plain `str` does not satisfy the protocol, so the handler refused it — mypy was
    #: right, and the declaration is what makes this a provider the factory will accept.
    name: ProviderName = "scripted"

    async def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
        *,
        budget: int | None = None,
    ) -> AssistantTurn:
        self.seen.append(list(messages))
        return AssistantTurn(text="noted", stop_reason="end_turn")

    def system_prompts(self) -> list[str]:
        return [
            message.content for turn in self.seen for message in turn if message.role == "system"
        ]


@dataclass
class NeverCalledExecutor:
    """The turn never calls a tool, and if it does the test should hear about it loudly."""

    async def call(
        self, name: str, arguments: dict[str, Any], *, timeout_s: float | None = None
    ) -> Any:
        raise AssertionError(f"no tool should have been called, saw {name}")


@pytest.fixture
def engine() -> Iterator[Engine]:
    built = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    register_sqlite_pragmas(built)
    Base.metadata.create_all(built)
    yield built
    built.dispose()


def _registry() -> ToolRegistry:
    """An empty catalog: the rules under test propose no calls."""
    return ToolRegistry.from_discoverable(_Published(()))


class _Published:
    """The ``_Discoverable`` shape: anything with a ``tools`` tuple."""

    def __init__(self, tools: tuple[McpTool, ...]) -> None:
        self._tools = tools

    @property
    def tools(self) -> tuple[McpTool, ...]:
        return self._tools


def _request(prompt: str, *, user_id: str, session_id: str) -> InvocationRequest:
    return InvocationRequest(prompt=prompt, user_id=user_id, session_id=session_id)


async def test_a_fact_from_one_session_reaches_the_next(engine: Engine, tmp_path: Path) -> None:
    """The assignment's cross-session requirement, driven through the real handler.

    Two different session ids, one user. The second turn must be answered with the name the first
    turn was told — which can only happen if the fact was stored *and* injected.
    """
    sessions = create_session_factory(engine)
    handler = InvocationHandlerFactory(
        provider=ScriptedProvider(),
        tools=_registry(),
        # No ignore: NeverCalledExecutor matches the executor protocol exactly, which is why the
        # one that used to be here became an error of its own ("unused type: ignore").
        executor=NeverCalledExecutor(),
        trace_dir=tmp_path,
    )

    with sessions() as session:
        await handler(_request("Hi, I'm Priya Raman.", user_id=USER, session_id=SESSION_A), session)
        session.commit()

    with sessions() as session:
        second = await handler(
            _request("what do you know about me?", user_id=USER, session_id=SESSION_B), session
        )
        session.commit()

    assert "Priya Raman" in second.output, (
        "the second session was not told what the first one learned"
    )


async def test_another_user_is_not_shown_the_first_users_facts(
    engine: Engine, tmp_path: Path
) -> None:
    """Isolation, asserted on the prompt the provider was actually given."""
    sessions = create_session_factory(engine)
    recorder = RecordingProvider()
    handler = InvocationHandlerFactory(
        provider=recorder,
        tools=_registry(),
        executor=NeverCalledExecutor(),
        trace_dir=tmp_path,
    )

    with sessions() as session:
        await handler(_request("I'm on the B team", user_id=USER, session_id=SESSION_A), session)
        session.commit()

    with sessions() as session:
        await handler(_request("hello", user_id=OTHER_USER, session_id="other-session"), session)
        session.commit()

    other_turns = [turn for turn in recorder.seen if turn[-1].content == "hello"]
    assert len(other_turns) == 1
    assert all(memory.FACTS_HEADING not in seen.content for seen in other_turns[0]), (
        "another user's prompt carried the first user's facts"
    )


async def test_the_fact_survives_a_new_database_session(engine: Engine, tmp_path: Path) -> None:
    """Durability, not caching: the fact is read back from the store, not held in memory."""
    sessions = create_session_factory(engine)
    recorder = RecordingProvider()
    handler = InvocationHandlerFactory(
        provider=recorder,
        tools=_registry(),
        executor=NeverCalledExecutor(),
        trace_dir=tmp_path,
    )

    with sessions() as session:
        await handler(_request("my name is Dana", user_id=USER, session_id=SESSION_A), session)
        session.commit()

    recorder.seen.clear()
    with sessions() as session:
        await handler(_request("hello again", user_id=USER, session_id=SESSION_B), session)
        session.commit()

    assert any(memory.FACTS_HEADING in prompt for prompt in recorder.system_prompts())
    assert any("Dana" in prompt for prompt in recorder.system_prompts())


async def test_a_turn_that_states_nothing_leaves_the_prompt_alone(
    engine: Engine, tmp_path: Path
) -> None:
    """A first turn has nothing remembered, so its system prompt is the base prompt unchanged."""
    sessions = create_session_factory(engine)
    recorder = RecordingProvider()
    handler = InvocationHandlerFactory(
        provider=recorder,
        tools=_registry(),
        executor=NeverCalledExecutor(),
        trace_dir=tmp_path,
    )

    with sessions() as session:
        await handler(
            _request("find the printer ticket", user_id=USER, session_id=SESSION_A), session
        )
        session.commit()

    assert recorder.system_prompts()
    assert all(memory.FACTS_HEADING not in prompt for prompt in recorder.system_prompts())
