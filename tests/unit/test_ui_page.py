"""The page, pinned where it can be pinned without a browser.

Two ordering claims, both of which were wrong when a reviewer looked at the running page rather
than at a test. The transcript was built newest-first, so each tool row sat above the prompt that
caused it and the agent appeared to answer the *next* question; and the approval card rendered
above the whole conversation, so "Approval needed" was the first thing on the page with nothing
above it explaining why.

Behaviour needs a browser, and the capture task in `devtasks` drives a real one. *Position* does
not, and position is what was broken — a page assembled as a string can be asserted on as a string,
which makes these the cheapest possible regression guards for two bugs that cost a reviewer's trust
in the transcript.
"""

from __future__ import annotations

from agent_service.ui import INDEX_HTML


def test_the_approval_area_comes_after_the_transcript() -> None:
    """A proposal belongs with the conversation it came out of, at the end of it.

    Rendering it above the transcript put a card for "create_ticket" above the user's first
    message — a decision presented before the exchange that led to it.
    """
    assert INDEX_HTML.index('id="transcript"') < INDEX_HTML.index('id="pending"')


def test_new_turns_are_appended_rather_than_prepended() -> None:
    """The guard for the transcript that read bottom-up.

    Prepending is what put every new turn at the top, so the newest exchange appeared above the
    one before it and each reply sat above its own prompt. The comment above `appendTurn` explains
    it; this asserts the code does it.
    """
    appended = INDEX_HTML.index("function appendTurn")
    body = INDEX_HTML[appended : appended + 400]

    assert "appendChild" in body
    assert "insertBefore" not in INDEX_HTML, "nothing may be inserted ahead of existing turns"


def test_the_new_session_control_exists_and_says_it_deletes_nothing() -> None:
    """The button a reviewer asked for, and the promise it must not overstate.

    It clears the screen and mints a session; it cannot delete a ticket, because the agent has no
    tool that removes one. The page says so in as many words rather than leaving a reader to
    wonder whether "new session" means "throw it away".
    """
    assert 'id="newsession"' in INDEX_HTML
    assert "no ticket" in INDEX_HTML


def test_the_model_is_named_in_the_header() -> None:
    """Otherwise the deterministic provider's canned sentences read as the product's best effort."""
    assert 'id="model"' in INDEX_HTML
    assert "api/info" in INDEX_HTML
