# The tool catalogue

What the model can actually call, how those tools are named and described, what a failure looks like
from the model's side, and how the MCP server authenticates to the mock. The assignment asks for
each of those explicitly, and this is the reference for all four.

## The four tools

The MCP server publishes exactly four tools (`services/mcp-ticketing/src/ticketing_mcp/tools.py`).
Two read and two write:

| Tool | Kind | Required arguments | Returns |
|---|---|---|---|
| `search_tickets` | read | none — all filters optional | one page of tickets plus `total`, `page`, `page_size`, `has_more` |
| `get_ticket` | read | `ticket_id` | one ticket, including its comments |
| `create_ticket` | **write** | `client_request_id`, `title`, `requester` | the created ticket, plus an `idempotency` block |
| `add_comment` | **write** | `client_request_id`, `ticket_id`, `author`, `body` | the comment, plus an `idempotency` block |

`search_tickets` filters on `q`, `status`, `priority`, `category`, `requester`, `page` and
`page_size` (max 50). It returns `total` and `has_more` on purpose: without them a model sees ten
rows and cannot tell "these are all the matches" from "these are the first ten of forty", and will
state the wrong thing with confidence. The eval case `pagination_page_two` is about exactly that
distinction.

### Naming

Names are `snake_case`, verb-first, and describe the **action on the domain object** rather than the
HTTP method underneath: `search_tickets`, not `get_tickets` or `list`; `create_ticket`, not
`post_ticket`. The model is selecting from a menu, and the fastest thing for it to compare is what
each entry *does*.

Two names carry information a shorter name would lose. `search_tickets` is a search rather than a
listing, because it takes a free-text query and returns a page — a model that reads "search" knows
the result may be partial. `add_comment` names its target through a separate `ticket_id` argument
rather than folding it into the verb, so the required argument is predictable from the name.

### Descriptions

Each description is one paragraph written **for a model**, and each answers the same four questions
in the same order: what it does, what it returns, when to use it, and what it does *not* do. The
pagination note is representative:

> Find tickets. Returns one page plus the arithmetic needed to describe it. …
> `total` and `has_more` are included in the payload on purpose. Without them a model sees ten
> rows and cannot tell "these are all the matches" from "these are the first ten of forty", and
> will state the wrong thing with confidence.

Descriptions are not restated in the agent. `ToolRegistry.as_provider_tools` passes them through
verbatim, because the MCP server wrote them for a model to read and a second copy in the agent would
be two versions that could disagree.

### Which tool is a write — derived, not listed

`services/agent/src/agent_service/tools/registry.py` decides `requires_approval` from
`McpTool.is_write`, which it derives from the tool's own published schema requiring
`client_request_id`. That chain matters: the same fact the MCP server enforces, and that its own
tests assert, is the fact the agent's gate branches on. A hand-maintained list of dangerous tools
would be a second source of truth and would drift the moment somebody added a write to the server.

The consequence is worth stating because it is the whole safety argument: **the write tools are a
consequence of the schema, not a category somebody remembered to mark.** Adding a fifth write tool
to the server makes it gated automatically.

`requires_approval` raises for an unknown name rather than returning a default. A default of `False`
would let a typo, or a tool renamed on the server, turn a write into an unattended one — the failure
that matters most here, and the one a default would hide.

## How a failure reaches the model

Failures are **returned, not raised**. Every tool function returns a `ToolOutcome` with `is_error`
set, which the server turns into `CallToolResult.is_error`. A raised exception would cross the
protocol boundary as a transport fault and the agent would lose the only thing it needs to recover:
*which kind* of failure this was.

The taxonomy is six kinds, each with a sentence written for a model to act on:

| Kind | What the model is told | What it should do |
|---|---|---|
| `NotFound` | "No ticket with that id exists. Check the id before retrying." | stop; the input was wrong |
| `Validation` | "The request was rejected: … Fix the arguments." | correct the call |
| `Authentication` | "The ticketing API rejected the credential. This is a configuration problem, not a retry." | stop; retrying cannot help |
| `RateLimited` | "The ticketing API is rate limiting this client. Waiting is required before retrying." | wait, then retry |
| `IdempotencyConflict` | "This client_request_id was already used for a different request." | use a new key for a new action |
| `Unresolved` | "The write's outcome could not be confirmed and it was NOT retried, because it may have succeeded. Check the ticket list before deciding whether to try again." | verify before repeating |

Three properties of that table are the substance of the design:

**Each sentence is advice, not a status code.** A model told "429" retries immediately; a model told
"waiting is required before retrying" is less likely to. The vocabulary exists so the model can
choose correctly without knowing anything about HTTP.

**The exhausted-retry sentence replaces the per-kind one.** `_describe` checks `error.attempts > 1`
*before* any per-kind branch. "Waiting is required before retrying" is right for a single 429 and
actively wrong once the client has already backed off and tried four times, because it tells the
model to do the one thing that has just been established not to work. The screenshot in
[`docs/media/05-failure-handled.png`](../media/05-failure-handled.png) is this path: four attempts,
given up, and a message that says so.

**`Unresolved` is the honest failure.** When a write's response is lost, the outcome is genuinely
unknown. The tool does not retry it — the ticket may exist — and tells the model to check. This is
the one place where doing less is the correct answer, and the eval case
`failures_do_not_duplicate_a_write` is what asserts the ticket count afterwards.

The agent's side of that contract is in [`failure-handling.md`](failure-handling.md): retry policy,
backoff, the deadline, and what the turn reports when it gives up.

## Authentication to the mock

```
agent ──MCP──▶ mcp-ticketing ──HTTP + X-API-Key──▶ mock-ticketing
```

The mock requires `X-API-Key` on `/tickets` and `/admin` routes; its default is the starter kit's
documented `dev-key-123`, and `.env` carries the value for a deployment. Only `mcp-ticketing` holds
it: the agent has no ticketing credential at all, and reaches the mock **only** through this server.
That is not a stylistic boundary — it is why the agent cannot leak a credential it never has, and
why the compose file gives the key to one service.

Three details of that arrangement are deliberate:

- **The key is checked by the mock, in one middleware, before anything else.** An unauthenticated
  call is a 401 from the fixture, which the MCP server maps to `Authentication` and the model is
  told not to retry.
- **A credential never appears in a tool result, a log or a trace.** The sentences above are written
  without one, and there is a test that a canary key planted in the environment never appears in a
  provider request or a response (`tests/unit/test_providers.py`).
- **The MCP transport differs by environment, not by code.** Streamable HTTP in compose (the server
  is a container); stdio on the host (the server is a child process). `devtasks serve` sets the
  transport and the loopback URL, because a run that inherited the compose values would try to
  resolve the hostname `ticketing` from outside the compose network and fail — which is how that was
  found.

## What the tools deliberately do not do

- **No tool lists every ticket.** `search_tickets` always paginates, so a conversation cannot
  silently pull the whole table into a model's context.
- **No update or delete tool.** The assignment's shape is "look things up, then make one change";
  `create_ticket` and `add_comment` are the two that shape needs, and an unreviewed delete path
  would be risk the task did not ask for.
- **No tool that takes a `user_id`.** The mock has no notion of users; the agent's user is the
  agent's business. Passing one through would imply an authorization the fixture does not perform.
