"""Which messages count as an answer to a proposal.

`CONTRACT.md` leaves this policy to us, so it is written down in one place and tested as a table
rather than argued case by case. The stakes are asymmetric and the tests say so: a vocabulary that
is too small costs one extra sentence asking the user to confirm, while one that is too loose means
a ticket was created by a message that was not clearly asking for one.

The cases are therefore of three kinds — clear agreements, clear refusals, and the messages that
must *not* be read as either. The third group is the one that matters: "yes, but first show me the
printer ticket" is a request for something else, and treating it as consent is the kind of mistake a
user only discovers after the write has happened.
"""

from __future__ import annotations

import pytest

from agent_service.agent.decisions import classify, normalise


@pytest.mark.parametrize(
    "prompt",
    [
        "yes",
        "Yes",
        "YES",
        "y",
        "yeah",
        "yep",
        "ok",
        "okay",
        "sure",
        "approve",
        "approved",
        "confirm",
        "confirmed",
        "proceed",
        "do it",
        "go ahead",
        "yes please",
        "yes, go ahead",
        "Yes, go ahead.",
        "ok thanks",
        "sure, create the ticket",
        "yes do that",
        # Politeness in front of a decision. "please" alone agrees to nothing, so it is skipped as
        # a lead word and judged by whatever follows it.
        "please proceed",
        "please do it",
    ],
)
def test_agreement_is_read_as_approval(prompt: str) -> None:
    assert classify(prompt) == "approve"


@pytest.mark.parametrize(
    "prompt",
    [
        "no",
        "n",
        "nope",
        "No.",
        "don't",
        "dont",
        "stop",
        "cancel",
        "decline",
        "declined",
        "reject",
        "never mind",
        "no thanks",
        "no, cancel it",
        "don't do it",
        "don't create the ticket",
        "nope, stop",
        "please don't",
    ],
)
def test_refusal_is_read_as_a_decline(prompt: str) -> None:
    assert classify(prompt) == "decline"


@pytest.mark.parametrize(
    "prompt",
    [
        "",
        "   ",
        "find tickets about the printer",
        "what is the status of TCK-1002?",
        "should I approve this?",
        "can you create a ticket?",
        "is TCK-1002 not closed yet?",
        # Mixed polarity. Unclear is not consent: whichever word came first, acting on this would
        # be a guess about what the user meant.
        "yes, but first show me the printer ticket",
        "no, wait — actually yes",
        "no, I mean yes",
        # A decision word inside a longer request. "yes, and also please reopen TCK-1042" agrees to
        # something *and* asks for something else; the honest reading is that it is not an answer to
        # the question that was asked.
        "yes, and also please reopen TCK-1042 while you are there",
        "go ahead and also search for the printer ticket",
    ],
)
def test_everything_else_is_neither(prompt: str) -> None:
    assert classify(prompt) is None


def test_an_apostrophe_and_its_absence_are_the_same_word() -> None:
    """A phone keyboard writes ' and a desktop writes ', and both must be understood.

    Found by writing this test rather than by reasoning: "don't" was in the vocabulary and "dont"
    was not, so a refusal typed on a phone would have been read as unrelated — and unrelated leaves
    the proposal pending, which to the user looks like being ignored.
    """
    assert classify("don't") == classify("dont") == "decline"
    assert classify("don\u2019t") == "decline"


def test_normalisation_flattens_case_punctuation_and_spacing() -> None:
    assert normalise("  Yes,   GO ahead!  ") == "yes go ahead"
    assert normalise("No.\nThanks!") == "no thanks"


def test_politeness_alone_is_not_agreement() -> None:
    """The line the "please" rule must not cross.

    Skipping "please" finds the decision in the word after it. If it were treated as a lead word
    instead, "please" on its own would approve a write — which is the one outcome this module exists
    to prevent.
    """
    assert classify("please") is None
    assert classify("please please") is None
