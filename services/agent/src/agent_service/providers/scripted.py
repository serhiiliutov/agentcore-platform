"""A deterministic, rule-driven provider — the default, and what CI and the evals run on.

This is not a stub that returns a fixed string. It reads the conversation and decides, so it
can drive the real loop through every path that matters: a search that finds something, a
search that finds nothing, a write that must be intercepted for approval, and a plain answer
with no tools. A program of canned replies could not do that, and a live model cannot do it
twice identically — which is exactly why this exists (ADR-0002).

The rules are ordered and first-match-wins, so behaviour is a property of the list rather than
of the model's mood. Each rule is a name, a predicate over the messages, and what to emit.
Because the predicates are pure functions of the conversation, the same input always produces
the same turn, and a failing eval can be re-run to the same failure.

The rule set deliberately includes one write. An eval that never proposes a write cannot test
the approval gate, and the approval gate is the requirement this project is scored on.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from agent_service.config import ProviderName

from .base import (
    AssistantTurn,
    Message,
    ToolCallRequest,
    ToolSchema,
)

#: Matches "TCK-1001" anywhere, so a rule can act on a referenced ticket.
TICKET_ID_PATTERN = re.compile(r"\bTCK-\d+\b", re.IGNORECASE)

#: Phrases meaning "open a ticket", in one place because both the matching rule and the title
#: cleanup need them — and a divergence between the two would leave the phrase in the title
#: the provider then sends to the server.
CREATE_PHRASES = (
    "open a ticket",
    "create a ticket",
    "raise a ticket",
    "new ticket",
    "file a ticket",
)

#: Fields the scripted provider fills in when it fabricates a write. Fixed rather than
#: generated, so two runs produce byte-identical arguments and the idempotency fingerprint in
#: Stage 4 has something stable to compare.
SCRIPTED_REQUESTER = "scripted-user"
SCRIPTED_PRIORITY = "medium"
SCRIPTED_CATEGORY = "other"


@dataclass(frozen=True)
class Rule:
    """One deterministic behaviour.

    ``matches`` receives the whole conversation, not just the last message, because a rule
    often needs to know whether a tool has already run — a search that returned nothing leads
    somewhere different from one that returned rows.
    """

    name: str
    matches: Callable[[Sequence[Message]], bool]
    emit: Callable[[Sequence[Message]], AssistantTurn]


@dataclass
class ScriptedProvider:
    """The rule-driven provider.

    ``rules`` is injectable so a test can drive a specific path without depending on the
    default set staying stable — and so an eval can supply its own program.
    """

    rules: tuple[Rule, ...] = ()
    #: How many replies this instance has produced, exposed for assertions.
    turns_taken: int = 0

    def __post_init__(self) -> None:
        if not self.rules:
            self.rules = DEFAULT_RULES

    @property
    def name(self) -> ProviderName:
        return "scripted"

    async def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
        *,
        budget: int | None = None,
    ) -> AssistantTurn:
        """First matching rule wins; the fallback is always a plain answer.

        ``tools`` is accepted and ignored. A scripted run chooses what to call from its own
        rules rather than from what is offered, which keeps an eval reproducible even if the
        server's tool list changes — and the loop still refuses to execute a name the server
        does not publish, so ignoring the list cannot cause an invalid call.
        """
        self.turns_taken += 1
        available = {tool.name for tool in tools}

        for rule in self.rules:
            if rule.matches(messages):
                turn = rule.emit(messages)
                # A rule that wanted a tool the server does not offer must not produce an
                # uncallable turn; fall through to a plain answer instead of failing later.
                if turn.tool_calls and not {call.name for call in turn.tool_calls} <= (
                    available or {call.name for call in turn.tool_calls}
                ):
                    continue
                return turn

        return AssistantTurn(
            text=_summarise(messages),
            stop_reason="end_turn",
        )


# --- Helpers the rules use ----------------------------------------------------


def _last_user_text(messages: Sequence[Message]) -> str:
    """The most recent thing the user said, lowercased."""
    for message in reversed(messages):
        if message.role == "user":
            return message.content.lower()
    return ""


def _tool_messages(messages: Sequence[Message]) -> list[Message]:
    """Every tool result so far, oldest first."""
    return [message for message in messages if message.role == "tool"]


def _has_tool_result(messages: Sequence[Message]) -> bool:
    return bool(_tool_messages(messages))


def _contains_any(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def _summarise(messages: Sequence[Message]) -> str:
    """A short, stable answer when no rule applies.

    Includes the user's own words so an eval can assert the provider saw the right prompt
    without inventing a natural-language generator here.
    """
    last_user = _last_user_text(messages)
    if not last_user:
        return "I have nothing to work from yet."
    return f"Understood: {last_user}"


# --- The default program ------------------------------------------------------


def _matches_create_ticket(messages: Sequence[Message]) -> bool:
    """A request to open a ticket, before any tool has run.

    Guarded on ``not _has_tool_result`` so the rule fires once: without it, the provider would
    request the same write on every subsequent turn and the loop would never terminate.
    """
    text = _last_user_text(messages)
    return not _has_tool_result(messages) and _contains_any(text, *CREATE_PHRASES)


def _emit_create_ticket(messages: Sequence[Message]) -> AssistantTurn:
    """Propose a ticket creation.

    Note the fixed ``client_request_id``. It is what makes a re-execution of this approved
    action provably the same action — the property Stage 4's ledger and Stage 8's gate both
    depend on, and one an eval must be able to drive deliberately.
    """
    text = _last_user_text(messages)
    return AssistantTurn(
        text="I will open a ticket for that.",
        tool_calls=(
            ToolCallRequest(
                id="scripted-call-create",
                name="create_ticket",
                arguments={
                    "client_request_id": "scripted-create-1",
                    "title": _title_from(text),
                    "requester": SCRIPTED_REQUESTER,
                    "priority": SCRIPTED_PRIORITY,
                    "category": SCRIPTED_CATEGORY,
                },
            ),
        ),
        stop_reason="tool_use",
    )


def _title_from(text: str) -> str:
    """A ticket title derived from the prompt, inside the server's 3-120 character bound.

    Bounded rather than truncated silently at the call site: the server rejects a title of two
    characters, and an eval that fails there would be testing the title helper rather than the
    loop.
    """
    cleaned = text.strip().rstrip(".!?")
    for phrase in CREATE_PHRASES:
        cleaned = cleaned.replace(phrase, "")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) < 3:
        cleaned = "scripted ticket"
    return cleaned[:120]


def _matches_search(messages: Sequence[Message]) -> bool:
    """A search request, before any search has run."""
    text = _last_user_text(messages)
    return not _has_tool_result(messages) and _contains_any(
        text, "find", "search", "list", "show me", "any tickets", "look for"
    )


def _emit_search(messages: Sequence[Message]) -> AssistantTurn:
    text = _last_user_text(messages)
    return AssistantTurn(
        text="Searching for matching tickets.",
        tool_calls=(
            ToolCallRequest(
                id="scripted-call-search",
                name="search_tickets",
                arguments={"q": _query_from(text)},
            ),
        ),
        stop_reason="tool_use",
    )


def _query_from(text: str) -> str:
    """The most useful keyword in the prompt, bounded to something the mock will match.

    A whole sentence would match nothing against the seed data, so the rule picks a term
    rather than passing the prompt through — an eval that always searches for a sentence would
    always see zero results and would never exercise the found-rows path.

    The filler list matters more than it looks. Picking the first long word from "find tickets
    about the printer" yields "about", which is a real search term only in the sense that the
    API will accept it — it matches nothing and would make every scripted search look empty.
    A test asserts the meaningful word comes back instead.
    """
    words = [word for word in re.split(r"\W+", text) if len(word) > 3]
    return next((word for word in words if word not in _SEARCH_FILLER), "")


#: Words that carry the request but not the subject. Kept as a set rather than inline so the
#: intent — "these are not what we are searching for" — is legible.
_SEARCH_FILLER = frozenset(
    {
        "find",
        "search",
        "list",
        "show",
        "looking",
        "look",
        "tickets",
        "ticket",
        "about",
        "with",
        "from",
        "that",
        "this",
        "there",
        "where",
        "when",
        "what",
        "which",
        "have",
        "has",
        "need",
        "want",
        "please",
        "help",
        "give",
        "some",
        "any",
        "all",
        "into",
    }
)


def _matches_get_ticket(messages: Sequence[Message]) -> bool:
    """A prompt that names a specific ticket, before any lookup has run."""
    text = _last_user_text(messages)
    return not _has_tool_result(messages) and bool(TICKET_ID_PATTERN.search(text))


def _emit_get_ticket(messages: Sequence[Message]) -> AssistantTurn:
    match = TICKET_ID_PATTERN.search(_last_user_text(messages))
    ticket_id = match.group(0).upper() if match else "TCK-1001"
    return AssistantTurn(
        text=f"Looking up {ticket_id}.",
        tool_calls=(
            ToolCallRequest(
                id="scripted-call-get",
                name="get_ticket",
                arguments={"ticket_id": ticket_id},
            ),
        ),
        stop_reason="tool_use",
    )


def _matches_answer_after_tools(messages: Sequence[Message]) -> bool:
    """Anything after at least one tool result: report rather than call again.

    This is the loop's terminating rule. Without it a scripted run would keep calling tools
    until the step ceiling, which would make every eval end in ``max_steps`` and prove
    nothing.
    """
    return _has_tool_result(messages)


def _emit_answer_after_tools(messages: Sequence[Message]) -> AssistantTurn:
    results = _tool_messages(messages)
    failed = any("ERROR:" in result.content for result in results)
    if failed:
        text = "That did not work, and I am not going to keep retrying it."
    else:
        text = f"Done. I used {len(results)} tool call(s) to answer."
    return AssistantTurn(text=text, stop_reason="end_turn")


#: Ordered, first-match-wins. The order encodes the decision procedure, so it is worth
#: reading top to bottom: explicit ticket reference, then a write, then a search, then report.
DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("create_ticket", _matches_create_ticket, _emit_create_ticket),
    Rule("get_ticket", _matches_get_ticket, _emit_get_ticket),
    Rule("search_tickets", _matches_search, _emit_search),
    Rule("answer_after_tools", _matches_answer_after_tools, _emit_answer_after_tools),
)
