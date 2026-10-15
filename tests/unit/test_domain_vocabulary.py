"""The ticket vocabulary the model is told about, pinned against the system that enforces it.

`agent_service.agent.domain` deliberately duplicates the enums in `ticketing_mcp.models`: the agent
reaches the ticketing system only over MCP, so importing the other service's package would cross the
boundary the whole design rests on. A duplicate is only acceptable if something fails when the two
drift apart — and this is that something.

The last test is the one the prompt rewrite exists for. A live model given no boundary answers an
unrelated question from its own knowledge; the assertion is that the prompt *says* not to, and that it
gives the model the closed value sets it would otherwise invent values around.
"""

from __future__ import annotations

import typing

from agent_service.agent import domain
from agent_service.agent.handler import SYSTEM_PROMPT
from ticketing_mcp import models


def test_the_agents_copy_matches_the_ticketing_systems_enums() -> None:
    """The mirror is checked, not trusted. A value added to one side makes this fail."""
    assert typing.get_args(models.Status) == domain.STATUSES
    assert typing.get_args(models.Priority) == domain.PRIORITIES
    assert typing.get_args(models.Category) == domain.CATEGORIES


def test_the_vocabulary_block_lists_every_value() -> None:
    """Rendered from the tuples, so a value cannot be listed in one and missing from the other."""
    for value in (*domain.STATUSES, *domain.PRIORITIES, *domain.CATEGORIES):
        assert f"`{value}`" in domain.VOCABULARY


def test_the_agent_offers_only_the_categories_it_can_create_with() -> None:
    """Reads are wider than writes on purpose.

    The seeded data contains a ticket categorised ``infrastructure``, which the API will report back
    but would reject on a create. The prompt must offer the narrower set, so the model does not choose
    a value the write path cannot accept.
    """
    assert "infrastructure" not in domain.CATEGORIES
    assert "other" in domain.CATEGORIES


def test_the_system_prompt_carries_the_vocabulary_and_the_scope_rule() -> None:
    """Both halves of the prompt rewrite, asserted rather than assumed."""
    assert domain.VOCABULARY in SYSTEM_PROMPT
    # Scope: tickets and nothing else, with an explicit instruction not to search to look busy.
    assert "nothing else" in SYSTEM_PROMPT
    assert "do not call a tool to look busy" in SYSTEM_PROMPT
    # The original behavioural rules survive the rewrite.
    assert "Use a tool before stating a fact" in SYSTEM_PROMPT
    assert "client_request_id" in SYSTEM_PROMPT
