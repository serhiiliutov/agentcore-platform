"""The ticket vocabulary the agent is told about, stated once.

Two things are true at the same time, and this module exists because of both.

**The vocabulary belongs to the ticketing system, not to the agent.** ``Status``, ``Priority`` and
``Category`` are defined in ``ticketing_mcp.models`` and enforced by the MCP server and the mock.
This module *duplicates* them rather than importing them, deliberately: the agent reaches the
ticketing system only over MCP, and importing its package would make the agent depend on the MCP
service's package — the boundary the whole design rests on. A copy pinned by a test is the price of
keeping that boundary, and it is worth paying for the one thing a model cannot infer: the exact
strings the tools accept.

**This is model-facing text, not validation.** Nothing here rejects an argument — the MCP server and
the mock are the authority on that. What it is for is telling the model, in its system prompt,
which values exist, so it does not have to guess between "high" and "urgent" or invent a status. A
model that knows the closed set picks from it; a model that does not, invents.
"""

from __future__ import annotations

#: A ticket's status. ``open`` and ``in_progress`` are live work; ``resolved`` and ``closed`` are
#: finished. Mirrors ``ticketing_mcp.models.Status``.
STATUSES: tuple[str, ...] = ("open", "in_progress", "resolved", "closed")

#: How urgent a ticket is. Mirrors ``ticketing_mcp.models.Priority``.
PRIORITIES: tuple[str, ...] = ("low", "medium", "high", "urgent")

#: What a ticket is about, for the values a *create* may send. Mirrors
#: ``ticketing_mcp.models.Category``.
#:
#: This is the set to create *with*, not the set to expect back: the seeded data contains a ticket
#: whose category is ``infrastructure``, which is outside it, so a search result may legitimately
#: report a category the model could not have chosen.
CATEGORIES: tuple[str, ...] = ("hardware", "software", "network", "access", "other")


def _options(values: tuple[str, ...]) -> str:
    """The values as a readable, comma-separated, backticked list."""
    return ", ".join(f"`{value}`" for value in values)


#: The block the system prompt embeds. Built from the tuples above rather than written out beside
#: them, so a value added to one cannot go missing from the other.
VOCABULARY: str = (
    "The ticketing system's closed value sets. Use these exact strings:\n"
    f"- status: {_options(STATUSES)} — `open` and `in_progress` are the live ones\n"
    f"- priority: {_options(PRIORITIES)}\n"
    f"- category (for a new ticket): {_options(CATEGORIES)}"
)
