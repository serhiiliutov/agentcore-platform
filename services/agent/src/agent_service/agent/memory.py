"""Cross-session memory: what is remembered from a conversation, and what is shown back.

`FactRepository` stores durable facts and is fully tested. What it did not have was a *caller*:
nothing in the request path ever wrote a fact from a conversation or read one into a prompt, so
the assignment's "at least one cross-session fact" was met by a table and a repository rather
than by the agent. This module is the missing half — a producer and a consumer — and the eval
suite's `memory_*` cases are what found the gap.

Three decisions, each of which is a way this could go wrong:

**Extraction is deterministic, not a model call.** The keys are a fixed whitelist, so the only
thing a model could add is a second way to be wrong. A regex over the user's own words is
reviewable, testable without a provider, and identical on every run — which is what lets an eval
assert on it. The plan allowed an optional model extractor; it is not built, and this note is why.

**Only the user's prompt is read.** Not the assistant's reply, not a tool result, not a ticket
body. That is the whitelist's real job: the danger in remembering is not a missing fact, it is a
ticket description or a quoted email becoming durable state about a person. Reading one field with
three known keys is a rule that cannot leak, while "read the conversation and remember what looks
important" is a rule that cannot be checked.

**Injection goes in the system prompt, and nothing is written back to the transcript.** The turn
that gets stored is the prompt the user typed, so the conversation the next turn reads is the one
that happened — not one with a memory preamble in it. Injection is per turn and derived, which
means a fact that expires stops appearing without anything having to be deleted.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from agent_service.db.repositories.facts import DEFAULT_TTL_DAYS, FactRepository

#: The whole vocabulary of what may be remembered. A tuple rather than a set so a reviewer reads
#: it in a stable order, and named here rather than inline so the extractor, the renderer and the
#: tests all agree on one list.
FACT_KEYS: tuple[str, ...] = ("requester_name", "team", "preferred_category")

#: What the model is told the block is. Shared with the scripted provider's "what do you know"
#: rule, which is how an eval observes injection over HTTP without a live model.
FACTS_HEADING = "What you know about this user from earlier conversations:"

#: Names, from the three phrasings people actually use. Bounded to two words, because a longer
#: capture is a sentence and a sentence in a name field is the beginning of a memory that quotes.
_NAME_PATTERN = re.compile(
    r"\b(?:my name is|i am|i'm)\s+([A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*)?)",
    re.IGNORECASE,
)

#: A word that can start a sentence the name rule would otherwise misread as a name. "I'm on the
#: B team" and "I'm looking into the printer" both match the name pattern, and neither is a name.
_NAME_STOPWORDS = frozenset(
    {
        "on",
        "in",
        "from",
        "with",
        "at",
        "the",
        "a",
        "an",
        "not",
        "here",
        "there",
        "new",
        "looking",
        "fine",
        "good",
        "ok",
        "okay",
        "going",
        "trying",
        "sorry",
        "still",
        "just",
        "now",
        "ready",
        "interested",
        "having",
        "seeing",
        "getting",
        "told",
        "back",
        "out",
        "up",
    }
)

#: Teams. Two optional words after the phrase, so "the B team" and "the platform security" both
#: match. The trailing " team" is removed in `extract` rather than excluded here, because a
#: lookahead inside the second-word group is harder to read than the sentence it saves.
_TEAM_PATTERN = re.compile(
    r"\b(?:my team is|i work (?:on|in|with)|i'?m on|i am on)\s+(?:the\s+)?"
    r"([A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*)?)",
    re.IGNORECASE,
)

#: One word after "I prefer". Bounded to something a category could be, and to one token so a
#: preference for a sentence does not become a stored value.
_PREFERENCE_PATTERN = re.compile(r"\bi prefer\s+([A-Za-z][A-Za-z\-]{1,20})\b", re.IGNORECASE)


def extract(text: str) -> dict[str, str]:
    """The whitelisted facts stated in ``text``, keyed by name.

    Returns only keys from `FACT_KEYS`, and only when the value looks like the kind of thing that
    key holds. An empty mapping is the normal result for a prompt that states nothing, which is
    most of them.
    """
    found: dict[str, str] = {}

    team = _first_group(_TEAM_PATTERN, text)
    if team:
        # "the B team" captures "B team"; the word is the sentence's, not the team's. Asserted by
        # a test, because the first version of this pattern claimed to handle it and did not.
        found["team"] = _titleish(_drop_trailing_word(team, "team"))

    name = _first_group(_NAME_PATTERN, text)
    if name and name.split()[0].casefold() not in _NAME_STOPWORDS and "team" not in found:
        # Not recorded when a team was matched from the same phrase: "I'm on the B team" is a
        # team, and the name rule's capture of it is the misreading the stopwords exist for.
        found["requester_name"] = _titleish(name)

    preference = _first_group(_PREFERENCE_PATTERN, text)
    if preference:
        found["preferred_category"] = preference.casefold()

    return found


def remember(
    facts: FactRepository,
    *,
    user_id: str,
    session_id: str,
    prompt: str,
    ttl_days: int | None = None,
) -> dict[str, str]:
    """Store whatever ``prompt`` states, and return what was stored.

    Returns the mapping so a caller can log or trace it without re-extracting. ``ttl_days`` is
    passed through when the caller supplies one, so the settings decide how long a fact lives;
    when it is absent the repository's own default applies.
    """
    stated = extract(prompt)
    for key, value in stated.items():
        facts.remember(
            user_id=user_id,
            key=key,
            value=value,
            source_session_id=session_id,
            # The repository's own default, named rather than left to omission. This call used to
            # assemble a one-key dict and unpack it so the argument could be *absent* — hard to
            # read, and, as mypy pointed out, not type-safe either: a `**` expansion is not
            # checked against keyword-only parameters, so a misspelt key would have been a silent
            # no-op. Passing the value the repository would have defaulted to is the same
            # behaviour with the reasoning visible.
            ttl_days=ttl_days if ttl_days is not None else DEFAULT_TTL_DAYS,
        )
    return stated


def render(facts: Mapping[str, str]) -> str:
    """The block to add to a system prompt, or an empty string when there is nothing to add.

    Empty rather than a heading with nothing under it: a heading is an instruction to the model
    that something follows, and a model told to expect facts that are not there will sometimes
    supply them.
    """
    known = {key: facts[key] for key in FACT_KEYS if facts.get(key)}
    if not known:
        return ""

    lines = "\n".join(f"- {key}: {value}" for key, value in known.items())
    return f"{FACTS_HEADING}\n{lines}"


def with_facts(system_prompt: str, facts: Mapping[str, str]) -> str:
    """A system prompt with this user's current facts appended, if there are any."""
    block = render(facts)
    if not block:
        return system_prompt
    return f"{system_prompt}\n\n{block}"


def _first_group(pattern: re.Pattern[str], text: str) -> str:
    """The first captured group, trimmed, or an empty string."""
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def _titleish(value: str) -> str:
    """``priya raman`` as ``Priya Raman``, without lowercasing the rest of a mixed-case input."""
    return " ".join(word[:1].upper() + word[1:] for word in value.split())


def _drop_trailing_word(value: str, word: str) -> str:
    """``value`` without a final ``word``, case-insensitively, or unchanged if it has none."""
    parts = value.split()
    if len(parts) > 1 and parts[-1].casefold() == word:
        return " ".join(parts[:-1])
    return value
