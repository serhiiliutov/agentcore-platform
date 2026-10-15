"""Typed shapes for the ticketing API, taken from the contract the mock actually has.

These are pydantic models rather than loose dicts for one reason that matters beyond
tidiness: the MCP tool schemas are generated from them. A hand-written JSON schema and
a hand-written parser drift apart; one set of models means the schema the model sees and
the validation applied to its arguments cannot disagree.

Every field here was observed by `tests/contract/`, not assumed:

* the list view omits ``comments`` while the detail view includes it — hence two models
  rather than one with an optional field, so "did I fetch the comments?" is a type
  question rather than a ``if "comments" in ticket`` question.
* ``assignee`` is genuinely nullable on creation, so it is ``str | None`` rather than a
  sentinel string like ``""``.
* timestamps arrive as ISO-8601 with a trailing ``Z``.

`extra="ignore"` throughout. The mock is the fixture and may add a field; a client that
raises on an unknown key would break on an additive change that cannot possibly hurt it.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

#: A ticket id as the API uses it, e.g. ``TCK-1001``.
TicketId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z]{3}-\d{3,}$")]

#: Closed sets the mock validates. An unknown value is a 422, not an empty result.
#:
#: This is a statement about what we **send**, which is why the read models below do not use
#: `Category`. The mock validates request parameters and bodies against these sets; it does not
#: promise that everything it reports back is inside them, and it is not: the seeded fixture
#: contains a ticket whose category is ``infrastructure``.
Status = Literal["open", "in_progress", "resolved", "closed"]
Priority = Literal["low", "medium", "high", "urgent"]
Category = Literal["hardware", "software", "network", "access", "other"]

#: The category as the API **reports** it — any string, deliberately.
#:
#: This was `Category` on the read models, and the difference is not hypothetical: one seeded ticket
#: (TCK-1020) has ``infrastructure``, so *every search whose page contained that ticket* failed to
#: parse. The failure did not look like a bad filter — the client reported "the API returned a body
#: that does not match SearchResult", the whole page was discarded for one row, and the agent saw a
#: tool error where it should have seen results. It surfaced through a query that had been reduced to
#: an empty string, which returns the unfiltered list; the query was a separate bug, and this one
#: would have hit any search broad enough to match TCK-1020.
#:
#: A value we only pass through cannot be validated against a set we invented. `extra="ignore"`
#: already applies that reasoning to fields the backend adds; this applies it to values it widens.
ReadCategory = str

#: Field length limits the mock enforces, so the tool schema can reject first and give
#: a better message than a 422 from three layers down.
TITLE_MIN, TITLE_MAX = 3, 120
DESCRIPTION_MAX = 2000
REQUESTER_MIN = 2
COMMENT_BODY_MIN, COMMENT_BODY_MAX = 1, 1000
COMMENT_AUTHOR_MIN = 2

#: The default page size the mock applies when none is given.
DEFAULT_PAGE_SIZE = 10
#: The largest page the mock accepts; anything more is a 422. The tool clamps to this.
MAX_PAGE_SIZE = 50


class _ApiModel(BaseModel):
    """Shared configuration: tolerate additive changes from the backend."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Comment(_ApiModel):
    """A comment on a ticket."""

    id: str
    author: str
    body: str
    created_at: str


class TicketSummary(_ApiModel):
    """A ticket as the *search* endpoint returns it.

    Note the absence of ``comments``. That is the mock's behaviour, and modelling it
    means the agent cannot accidentally promise a user a discussion thread it never
    fetched.
    """

    id: str
    title: str
    description: str
    status: Status
    priority: Priority
    # Reported, not requested: see `ReadCategory` for why this is wider than `Category`.
    category: ReadCategory
    requester: str
    assignee: str | None = None
    created_at: str
    updated_at: str


class Ticket(TicketSummary):
    """A ticket as the *detail* endpoint returns it, including its comments."""

    comments: list[Comment] = Field(default_factory=list)


class SearchResult(_ApiModel):
    """One page of search results, with the arithmetic that makes it honest.

    ``total`` and ``has_more`` are carried through to the tool result on purpose. A
    model given only a list of ten tickets cannot tell "these are all of them" from
    "these are the first ten of thirty", and will state the wrong thing with confidence.
    """

    items: list[TicketSummary]
    total: int
    page: int
    page_size: int
    has_more: bool


class NewTicket(_ApiModel):
    """The body of a create request, validated to the mock's own limits."""

    title: Annotated[str, StringConstraints(min_length=TITLE_MIN, max_length=TITLE_MAX)]
    description: Annotated[str, StringConstraints(max_length=DESCRIPTION_MAX)] = ""
    priority: Priority = "medium"
    category: Category = "other"
    requester: Annotated[str, StringConstraints(min_length=REQUESTER_MIN)]


class NewComment(_ApiModel):
    """The body of a comment request."""

    author: Annotated[str, StringConstraints(min_length=COMMENT_AUTHOR_MIN)]
    body: Annotated[
        str, StringConstraints(min_length=COMMENT_BODY_MIN, max_length=COMMENT_BODY_MAX)
    ]


class SearchQuery(_ApiModel):
    """The filters the search endpoint accepts.

    ``requester`` is an exact (case-insensitive) name match while ``q`` does a
    substring match across title and description. Stage 1 pinned that difference, and the
    tool description has to say it, or a model will use one where it meant the other.
    """

    q: str | None = None
    status: Status | None = None
    priority: Priority | None = None
    category: Category | None = None
    requester: str | None = None
    page: Annotated[int, Field(ge=1)] = 1
    page_size: Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE

    def as_query_params(self) -> dict[str, str | int]:
        """Drop unset filters so the mock applies only what was asked for.

        An explicit intermediate rather than returning the dump directly, so the
        narrower return type is a checked claim rather than an unchecked one.
        """
        params: dict[str, str | int] = self.model_dump(exclude_none=True)
        return params
