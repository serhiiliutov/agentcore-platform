"""Reading a user's *message* as an answer to a proposal.

`CONTRACT.md` is explicit that the platform's flow is conversational: "The user's answer arrives
as the next ``/invocations`` call in the same session (``{"prompt": "yes, go ahead"}``). Your
service decides what counts as approval, what counts as a decline, and what to do if the user asks
something unrelated instead."

This module is that decision, and it is the only place it is made. It is deliberately a pure
function of the text — no database, no model call — so an eval can assert on it and a reviewer can
read the whole policy in one screen.

Three choices are worth defending, because each is a way this could be wrong in a way nobody would
notice until a user was surprised.

**A closed vocabulary, matched on the whole message.** The alternative — substring matching, or
asking a model whether "that's fine I suppose" is agreement — makes approval a judgement call made
fresh each time. The cost of the vocabulary being too small is one extra sentence asking the user
to confirm; the cost of it being too loose is a write that ran when the user was saying something
else. The asymmetry decides it.

**The first word decides the polarity, and its whole message must agree.** "yes, go ahead" is an
approval; "yes, but find me the printer ticket first" is not, because it asks for something and the
approval would be a guess. A message that contains both polarities at all ("no, don't — actually
yes") is treated as unrelated for the same reason: the honest reading is that it is unclear, and
the safe response to unclear is to ask rather than to act.

**Unrelated is a first-class answer, not a failure.** The contract leaves it to us, and the loop
handles it: the proposal stays pending and the turn is answered normally. That is what makes a
digression safe — the eval case `digression_then_resume` is the same requirement stated as
behaviour.
"""

from __future__ import annotations

from typing import Literal

#: What a message can mean for an outstanding proposal. ``None`` from `classify` means "neither",
#: which is a real answer rather than an error.
Decision = Literal["approve", "decline"]

#: Words that may *start* an approval. Every one of these reads as agreement on its own.
_AFFIRMATIVE_LEAD = frozenset(
    {
        "yes",
        "y",
        "yeah",
        "yep",
        "yup",
        "ok",
        "okay",
        "sure",
        "approve",
        "approved",
        "confirm",
        "confirmed",
        "proceed",
        "go",
        "do",
    }
)

#: Words that may *start* a decline.
_NEGATIVE_LEAD = frozenset(
    {
        "no",
        "n",
        "nope",
        "dont",
        "stop",
        "cancel",
        "decline",
        "declined",
        "reject",
        "rejected",
        "never",
        "abort",
    }
)

#: Everything that may follow an approval's first word. Filler and restatement, plus the nouns a
#: person naturally repeats back ("create", "ticket") — and the verbs "go"/"do", so "go ahead" and
#: "do it" work even though neither second word is a decision by itself.
_AFFIRMATIVE_TAIL = frozenset(
    {
        "a",
        "add",
        "ahead",
        "and",
        "approve",
        "approved",
        "confirm",
        "confirmed",
        "create",
        "do",
        "for",
        "go",
        "it",
        "me",
        "now",
        "ok",
        "okay",
        "please",
        "proceed",
        "sure",
        "thanks",
        "thank",
        "that",
        "the",
        "this",
        "ticket",
        "with",
        "write",
        "y",
        "yeah",
        "yep",
        "yes",
        "you",
        "yup",
    }
)

#: Everything that may follow a decline's first word.
_NEGATIVE_TAIL = frozenset(
    {
        "a",
        "abort",
        "and",
        "add",
        "cancel",
        "create",
        "decline",
        "declined",
        "do",
        "dont",
        "go",
        "it",
        "mind",
        "never",
        "no",
        "not",
        "now",
        "please",
        "reject",
        "rejected",
        "stop",
        "thanks",
        "thank",
        "that",
        "the",
        "this",
        "ticket",
        "write",
        "you",
    }
)

#: Any of these anywhere in the message makes it a decline, however it starts. Used for the
#: ambiguity guard below rather than for the polarity, which the first word owns.
_NEGATIVE_ANYWHERE = frozenset(
    {
        "no",
        "not",
        "nope",
        "dont",
        "never",
        "stop",
        "cancel",
        "decline",
        "declined",
        "reject",
        "rejected",
        "abort",
    }
)

#: The same for approval. Deliberately shorter than the tail list: a word here has to mean
#: agreement by itself for the ambiguity guard to be meaningful.
_AFFIRMATIVE_ANYWHERE = frozenset(
    {
        "yes",
        "yeah",
        "yep",
        "yup",
        "ok",
        "okay",
        "sure",
        "approve",
        "approved",
        "confirm",
        "confirmed",
        "proceed",
    }
)

#: Characters that end a sentence rather than a word. Replaced by a space so "yes, go ahead"
#: tokenises rather than producing a token "yes," that no list contains.
_PUNCTUATION = str.maketrans(dict.fromkeys('.,!?;:()[]"`*_', " "))


def normalise(prompt: str) -> str:
    """``prompt`` as lowercase words: punctuation gone, curly apostrophes straightened.

    Public because a test asserts on it directly, and because the same normalisation is what a
    reader needs in order to check a vocabulary entry against the sentence it is meant to match.
    """
    text = prompt.casefold().replace("\u2019", "'").replace("'", "")
    return " ".join(text.translate(_PUNCTUATION).split())


def classify(prompt: str) -> Decision | None:
    """Whether ``prompt`` approves a proposal, declines one, or neither.

    ``None`` — neither — is the answer for every message this module is not sure about, including
    an empty one, a question, and anything mixing the two polarities. See the module docstring for
    why that is the deliberate default rather than a gap.
    """
    tokens = normalise(prompt).split()
    if not tokens:
        return None

    # "please" is politeness, not a decision. Skipped as the first word so "please proceed" and
    # "please don't" are read as the decisions they are — while "please" on its own stays
    # unrelated, because on its own it agrees to nothing. It is in both tail sets, so it may also
    # appear anywhere after the word that does decide.
    lead = 1 if tokens[0] == "please" and len(tokens) > 1 else 0

    # Checked before the polarity, so "no, I mean yes" and "yes, but no" are both unclear rather
    # than whichever word happens to come first. A guess here would be a write performed on a
    # message that did not clearly ask for one.
    if any(token in _NEGATIVE_ANYWHERE for token in tokens) and any(
        token in _AFFIRMATIVE_ANYWHERE for token in tokens
    ):
        return None

    first = tokens[lead]
    if first in _AFFIRMATIVE_LEAD and all(
        token in _AFFIRMATIVE_LEAD or token in _AFFIRMATIVE_TAIL for token in tokens
    ):
        return "approve"

    if first in _NEGATIVE_LEAD and all(
        token in _NEGATIVE_LEAD or token in _NEGATIVE_TAIL for token in tokens
    ):
        return "decline"

    return None
