"""What a tool result actually hands the model.

One property, and it is a regression guard for a defect that no test covered: the text placed in a
model's tool-result message must contain the **data** the tool returned, not only the sentence that
describes it. The MCP server populates both on purpose — `to_call_tool_result` says so in as many
words, "a tool that returned only one of them would be worse for half its callers" — and this
service forwarded the sentence alone.

The cost was not a terse answer but a fabricated one, and it is worth recording exactly, because it
is the kind of failure that reads as a model problem and is not one. Asked for a breakdown of
tickets by status and category, a live run received "10 of 25 matching ticket(s) on page 1." and then
one sentence per lookup — "Ticket TCK-1001: … [open] with 0 comment(s)." — and answered with
twenty-five tickets whose ids, titles, requesters and statuses it had invented; the fixture's
requesters are people's names, and the model produced `user1@example.com` through
`user25@example.com`. It also reported that no category information was available, for tickets that
carry a category, because that field had never reached it.

Both halves are asserted here, because the fix has two and either alone is incomplete: the data has
to arrive, and the short sentence has to stay available for the response — a page of JSON is not
something to put in a tool row a person is scanning.

Hermetic by construction: these are the shapes `ticketing_mcp.server.to_call_tool_result` builds,
assembled directly, so no server, no model and no network are involved.
"""

from __future__ import annotations

import json
from typing import Any

from agent_service.mcp.client import ToolCallResult


def _server_result(
    *, message: str, data: dict[str, Any], is_error: bool = False
) -> ToolCallResult:
    """One result exactly as the MCP server sends it: a sentence beside structured data.

    Both fields are filled the way `to_call_tool_result` fills them — `content` carrying the JSON
    body as text, `structured_content` carrying the same body — so the object under test is the one
    the client really builds rather than a convenient approximation of it.
    """
    body: dict[str, Any] = {"message": message, "data": data}
    return ToolCallResult(
        is_error=is_error,
        text=json.dumps(body, indent=2),
        structured=body,
    )


def test_the_model_is_given_the_returned_data_and_not_only_the_sentence() -> None:
    """The property the whole module exists for: search results arrive, fields and all."""
    result = _server_result(
        message="1 of 1 matching ticket(s) on page 1.",
        data={
            "items": [
                {
                    "id": "TCK-1005",
                    "title": "Printer on floor 3 keeps jamming",
                    "status": "open",
                    "category": "hardware",
                    "requester": "Aisha Khan",
                }
            ],
            "total": 1,
            "has_more": False,
        },
    )

    text = result.as_model_text()

    assert "1 of 1 matching ticket(s) on page 1." in text
    assert "TCK-1005" in text
    assert "Aisha Khan" in text
    # The field a reader asked to aggregate by, and the one the answer claimed was unavailable.
    assert '"category": "hardware"' in text


def test_a_tickets_comments_reach_the_model() -> None:
    """The question the agent could not answer: "can you read comments on this ticket?".

    The sentence carries the *count* and nothing else, so a model shown only the sentence can say
    how many comments a ticket has and never what any of them says. That is exactly the exchange
    that happened — "has 1 comment, but I was unable to retrieve the details".
    """
    result = _server_result(
        message="Ticket TCK-1025: Install Claude Desktop [open] with 1 comment(s).",
        data={
            "id": "TCK-1025",
            "title": "Install Claude Desktop",
            "comments": [
                {
                    "id": "CMT-1025-1",
                    "author": "Dana Whitfield",
                    "body": "Enrolled the new device; the old one is revoked.",
                }
            ],
        },
    )

    text = result.as_model_text()

    assert "1 comment(s)" in text
    assert "Enrolled the new device; the old one is revoked." in text


def test_a_failure_is_still_marked_as_one_with_the_payload_attached() -> None:
    """Prefixing survives the payload, so a refusal cannot read as an answer in a transcript.

    Worth asserting beside the new behaviour rather than assuming it: the prefix is what keeps
    "no ticket with that id exists" from being read as a search result, and it would be an easy
    thing to lose while widening what the method returns.
    """
    result = _server_result(
        message="No ticket with that id exists. Check the id before retrying.",
        data={"error": {"kind": "NotFound", "detail": "TCK-9999"}},
        is_error=True,
    )

    text = result.as_model_text()

    assert text.startswith("ERROR: ")
    assert "No ticket with that id exists" in text
    assert "NotFound" in text


def test_a_result_with_no_payload_is_just_the_sentence() -> None:
    """The fallback, and the reason it matters: no stray empty JSON block on a bare result.

    A tool that returns a sentence and nothing else should reach the model as that sentence. The
    guard is ``if payload`` rather than an unconditional append, and this is what fails if somebody
    "simplifies" it into one.
    """
    result = ToolCallResult(is_error=False, text="done", structured={"message": "done"})

    assert result.as_model_text() == "done"
