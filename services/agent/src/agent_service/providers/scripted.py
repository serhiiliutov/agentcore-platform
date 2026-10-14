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

One rule is here for its *duration* rather than its decision. A turn deadline is a wall-clock
allowance, and the only honest way to exhaust one is for something inside the turn to take too
long — so `slow_turn` waits before it answers. Putting that wait in the mock instead would have
made every case slower to prove one thing about the loop.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

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
    #: Seconds to wait before emitting, when the rule fires. Zero for every rule whose point is
    #: the decision it makes rather than the time it takes. Honoured in one place, in `chat`.
    delay_s: float = 0.0


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
                if rule.delay_s:
                    # The one place a delay is honoured, so "which rules are slow" is answerable
                    # by reading the rule list rather than by reading the provider.
                    await asyncio.sleep(rule.delay_s)
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


def _matches_invalid_enum(messages: Sequence[Message]) -> bool:
    """A prompt that asks, in as many words, for a value the server will reject.

    This rule exists so the refusal path is *reachable* by an eval. The mock's priority is a
    four-value literal, and a write carrying anything else is refused — which is the case worth
    having, because the defect it guards against is a refused write being recorded as executed.
    A provider that only ever sends valid arguments cannot produce that case at all.
    """
    return not _has_tool_result(messages) and "invalid priority" in _last_user_text(messages)


def _emit_invalid_enum(messages: Sequence[Message]) -> AssistantTurn:
    text = _last_user_text(messages)
    return AssistantTurn(
        text="I will open a ticket for that.",
        tool_calls=(
            ToolCallRequest(
                id="scripted-call-invalid",
                name="create_ticket",
                arguments={
                    "client_request_id": "scripted-invalid-1",
                    "title": _title_from(text),
                    "requester": SCRIPTED_REQUESTER,
                    # Not a value the server accepts, and deliberately so.
                    "priority": "impossible",
                    "category": SCRIPTED_CATEGORY,
                },
            ),
        ),
        stop_reason="tool_use",
    )


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


#: The phrase that makes a turn deliberately outlast its budget. Named as a constant so the eval
#: case and the rule cannot drift apart, and phrased as an instruction rather than as a marker so
#: the prompt a reviewer reads in `cases.json` explains itself.
SLOW_TURN_PHRASE = "take longer than the turn"

#: How long `slow_turn` waits. Comfortably longer than the sub-second budget the deadline case
#: configures, and short enough that the case costs about two seconds — a suite whose timeout
#: test takes as long as a real timeout is a suite people skip.
SLOW_TURN_DELAY_S = 2.0


def _matches_slow_turn(messages: Sequence[Message]) -> bool:
    """A prompt that asks the turn to be slow, before any tool has run.

    Guarded on ``not _has_tool_result`` like the other rules, so it cannot fire twice in one
    conversation and add its delay again.
    """
    return not _has_tool_result(messages) and SLOW_TURN_PHRASE in _last_user_text(messages)


def _emit_slow_turn(messages: Sequence[Message]) -> AssistantTurn:
    """Search, having first spent more than the turn had.

    Emitting a search rather than nothing is the point: the turn is cut short *while trying to do
    the work*, which is the situation the deadline exists for. A slow turn that emitted nothing
    would end as a plain answer and would prove nothing about the budget.
    """
    return _emit_search(messages)


def _matches_search(messages: Sequence[Message]) -> bool:
    """A search request, before any search has run."""
    text = _last_user_text(messages)
    return not _has_tool_result(messages) and _contains_any(
        text, "find", "search", "list", "show me", "any tickets", "look for"
    )


def _emit_search(messages: Sequence[Message]) -> AssistantTurn:
    text = _last_user_text(messages)
    arguments: dict[str, Any] = {"q": _query_from(text)}
    # Sent only when the prompt names a page. Defaulting every search to "page 1" would put a
    # parameter in the recorded arguments that the user never asked for, and the eval that checks
    # a page hint should be able to tell a requested page from a defaulted one.
    page = _page_from(text)
    if page > 1:
        arguments["page"] = page

    return AssistantTurn(
        text="Searching for matching tickets.",
        tool_calls=(
            ToolCallRequest(
                id="scripted-call-search",
                name="search_tickets",
                arguments=arguments,
            ),
        ),
        stop_reason="tool_use",
    )


#: "page 2", wherever it appears in a search request.
_PAGE_PATTERN = re.compile(r"\bpage\s+(\d+)\b", re.IGNORECASE)


def _page_from(text: str) -> int:
    """The page the prompt asked for, or 1."""
    match = _PAGE_PATTERN.search(text)
    return int(match.group(1)) if match else 1


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


def _facts_block(messages: Sequence[Message]) -> str:
    """Whatever the system prompt was told about this user, or an empty string.

    Imported inside the function rather than at module scope: `agent.memory` reaches into the
    database package, and importing it at import time would make every consumer of the provider
    package pull the persistence layer in with it. A function-local import has no such effect.
    """
    from agent_service.agent.memory import FACTS_HEADING

    for message in messages:
        if message.role == "system" and FACTS_HEADING in message.content:
            return message.content.split(FACTS_HEADING, 1)[1].strip()
    return ""


def _matches_recall(messages: Sequence[Message]) -> bool:
    """A question about what is remembered, at any point in the conversation.

    Deliberately not guarded on ``not _has_tool_result``: the question is answerable before or
    after a search, and requiring a clean conversation would make the rule unreachable in the
    multi-turn cases that need it.
    """
    return "what do you know" in _last_user_text(messages)


def _emit_recall(messages: Sequence[Message]) -> AssistantTurn:
    """Read back the memory block.

    This is the rule that makes cross-session memory *observable* over HTTP. Without it the only
    evidence that a fact reached the prompt would be inside the prompt, and an eval cannot assert
    on a prompt it cannot see. It is a scripted-provider affordance, not a claim that a real model
    would answer this way — but the thing it observes, that the fact arrived, is real.
    """
    block = _facts_block(messages)
    if not block:
        return AssistantTurn(
            text="I do not have anything remembered about you yet.",
            stop_reason="end_turn",
        )
    return AssistantTurn(text=f"Here is what I have on file:\n{block}", stop_reason="end_turn")


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
#: reading top to bottom: a deliberately slow turn, a question about memory, an explicit ticket
#: reference, then a write, then a search, then report.
#:
#: ``recall_facts`` is first among the *decisions* because it asks about the conversation itself
#: rather than about the ticket system, and every rule below it would otherwise answer that
#: question differently depending on whether a tool had already run. ``slow_turn`` precedes even
#: that, because a prompt that asks to be slow has asked for the delay and nothing else, and a
#: later rule would answer it before the delay could happen.
DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("slow_turn", _matches_slow_turn, _emit_slow_turn, delay_s=SLOW_TURN_DELAY_S),
    Rule("recall_facts", _matches_recall, _emit_recall),
    # Before `create_ticket`, and both are creates: the invalid-enum prompt also says "open a
    # ticket", so the more specific rule has to win.
    Rule("invalid_enum", _matches_invalid_enum, _emit_invalid_enum),
    Rule("create_ticket", _matches_create_ticket, _emit_create_ticket),
    Rule("get_ticket", _matches_get_ticket, _emit_get_ticket),
    Rule("search_tickets", _matches_search, _emit_search),
    Rule("answer_after_tools", _matches_answer_after_tools, _emit_answer_after_tools),
)
