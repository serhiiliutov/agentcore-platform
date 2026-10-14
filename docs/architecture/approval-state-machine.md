# The approval state machine

The assignment's central requirement is that the agent proposes a write, stops, and only writes
once a human has said yes — and that a pending proposal survives a restart of the service. This
document is the mechanism behind that: the states, the transitions, who is allowed to make each
one, and what makes the whole thing durable rather than decorative.

The rule the design is built around:

> **A write tool is never executed from the model's request.** A model asking to call
> `create_ticket` produces a row in `approvals` and nothing else. The only code that performs the
> call reads the row and executes the arguments *stored in it*.

## States

`ApprovalState` (`db/models/approval.py`) is a closed set, enforced by a `CHECK` constraint on the
column, so a typo like `"aproved"` fails at the database rather than producing an approval that can
never execute.

| State | Meaning | Terminal |
|---|---|---|
| `pending` | Waiting for a human. The only state a decision can act on. | no |
| `approved` | Allowed, not yet carried out. | no |
| `executed` | Carried out successfully. | yes |
| `failed` | Attempted and refused by the ticketing system. | yes |
| `declined` | A person said no. | yes |
| `expired` | Nobody decided before the deadline. | yes |

`declined` and `failed` are separate states on purpose. The first is a human decision and the second
is a backend refusal, and a user reading their own history needs to tell "I said no" from "it did
not work" — which is also why the response carries a distinct `outcome` for each.

`TERMINAL_STATES` is the set no transition may leave, and it is used by the repository rather than
re-derived at each call site.

## Transitions

| From | Event | To | Who does it |
|---|---|---|---|
| — | the model called a write tool | `pending` | `AgentLoop._propose_if_write` |
| `pending` | approved, execution succeeds | `executed` | `WriteExecutor.execute` |
| `pending` | approved, execution refused | `failed` | `WriteExecutor.execute` |
| `pending` | declined | `declined` | `ApprovalGate.decide` |
| `pending` | the deadline passed | `expired` | `ApprovalRepository.decide` |

Every other transition is refused by `ApprovalRepository.decide` and
`ApprovalRepository.claim_for_execution`, which throw `ApprovalTransitionError` — surfaced to HTTP
as a 409. That refusal is enforced in the repository, not in the route, so a second call site
cannot acquire the ability to decide twice merely by forgetting to check.

Two orderings in that table are deliberate and each was chosen against the alternative:

- **The decision is recorded before execution is attempted.** If the write then fails, the row is
  `failed` — a state a retry can reason about. Recording after would leave a window in which a
  crash left the row `pending` while the write had in fact happened, which is the one state that
  cannot be distinguished from "nothing happened".
- **Declining is decided in the gate, not below it.** The repository records states and has no
  opinion about consequences; the executor cannot run a declined row. So the single branch that
  says "a refusal does not execute" lives in `ApprovalGate.decide`, and it is the only branch there.

## The two-call protocol

A proposal ends the request that produced it. The decision arrives as a *later* request:

```
POST /invocations                      {"prompt": "the printer is jammed, raise a ticket"}
  → 200 {"stop_reason": "awaiting_approval",
         "pending_approval": {"approval_id": "...", "tool_name": "create_ticket",
                              "arguments": {...}, "expires_at": "..."},
         "output": "I'll create ... shall I go ahead?"}

POST /invocations/approvals/{id}/decide {"user_id": "...", "approved": true}
  → 200 {"state": "executed", "outcome": "executed", "detail": "Created ticket TCK-1025."}
```

The alternative — keeping the request open until the human answers — was rejected because it makes
the agent's availability depend on a person's attention, and because a runtime's request has a
deadline that a human's response does not respect. Ending the turn is also what makes the turn's
`stop_reason` honest: `awaiting_approval` is a *successful* turn that is waiting, not an error, and
`stop_reason` is never inferred from which fields happen to be populated.

### Two ways in, one gate

`CONTRACT.md` says the answer arrives as the next `/invocations` call, and the page needs a button
that acts on a *specific* approval. Both are supported, and both reach the same gate — which is the
only part of this that is not a matter of taste:

```
POST /invocations                        {"prompt": "yes, go ahead"}     ← the contract's flow
POST /invocations/approvals/{id}/decide  {"approved": true}              ← the page's button
        │                                        │
        └───────────────► ApprovalGate.decide ◄──┘   (one implementation, one row, one write)
```

The message path is `agent/decisions.py`: a pure function of the text, with a closed vocabulary and
the first word deciding the polarity. *"yes, go ahead"* approves; *"yes, but find me the printer
ticket first"* is read as unrelated rather than as consent, because acting on it would be a guess
about what the user meant. **Unrelated is a first-class answer** — the proposal stays pending, the
turn is answered normally, and the still-outstanding proposal is carried on the response so a client
keeps showing the user what is waiting for them.

The row is the authority either way in: whoever decides, execution reads the stored `action` and
`payload` and never the message that triggered it. The pairing is wired once, in
`factory.py::build_decision_callable`, so there is no second path to a write to keep in step.

Three eval cases assert the three outcomes over the real contract — `approval_by_message`,
`decline_by_message` and `unrelated_message_keeps_the_proposal` — and
`tests/unit/test_message_approval.py` asserts them against the same gate the button uses, including
the case where no gate is installed at all: the seam is optional, and its absence means the message
is just a message rather than a write nobody authorised.

(On AgentCore the decision route is not part of the Runtime contract and would need to become either
an invocation or a gateway route; that is item 3 in [`AGENTCORE.md`](../../AGENTCORE.md). The message
path needs nothing — it is an ordinary invocation.)

## What makes it survive a restart

The proposal is a row, and the row carries everything needed:

- `action` and `payload` — the tool name and the **exact arguments**. Execution never goes back to
  the model for parameters, so a provider outage, a new conversation or a changed prompt cannot
  change what gets written. Approving "create a ticket" and executing something else is impossible
  by construction.
- `session_id` and `user_id` — the conversation it belongs to, and the only user allowed to see or
  decide it.
- `client_request_id` — the idempotency key, unique on the table, minted **before** approval rather
  than inside the tool. Re-executing an approved action therefore reuses the key and cannot create a
  second ticket.
- `requested_at` / `expires_at` / `decided_at` / `executed_at` — the timeline.
- `result` / `error` — what came back, stored so a later question can be answered from our own
  record instead of another read.

Two things exist because of that paragraph and would otherwise be easy to miss:

**`GET /api/approvals?user_id=…`** lists outstanding proposals, loaded by the page *on load* rather
than only after a turn. Without it, a proposal made before a restart would be durable in the
database and invisible in the interface — the durability would be real and useless. It reuses the
same `PendingApproval` shape the invocation response carries, rather than defining a second shape
for one thing.

**`tests/unit/test_decision_route.py` and the `restart_between_propose_and_approve` eval case**
drive propose → restart → decide over the real HTTP contract. The eval case is the one that proves
the property end to end; the screenshot in
[`docs/media/03-proposed-write.png`](../media/03-proposed-write.png) is the same state, in the
interface.

## Expiry

`expires_at` is set when the proposal is created (`AGENT_APPROVAL_TTL_S`, 1800 by default — the
variable this document named as `..._TTL_MINUTES` for several stages after the code had moved to
seconds, which is the kind of drift a reader cannot detect and a grep would have caught) and
evaluated when a decision arrives: a `pending` row whose deadline has passed is moved to `expired`
instead of being executed late. An approval is a decision about *now* — a proposal left untouched
for a day should not create a ticket tomorrow, when the ticket may be pointless or the person may
not remember proposing it.

The page shows the deadline in local time with a relative hint. That display has its own small
correctness trap, documented in the code: the API sends a naive UTC timestamp, and a browser will
read a bare timestamp as local time unless it is told otherwise, which would show an expiry hours
out. A deadline displayed wrongly is worse than one not displayed, because a reader would trust it.

## Cancellation, and why it never interrupts a write

Cancelling a turn is cooperative (`agent/budget.py`, `agent/inflight.py`): a flag somebody sets,
checked at `_checkpoint()` **between** steps and never inside one. A cancel that landed mid-write
would re-open exactly the ambiguity the idempotency key exists to close — did the call reach the
backend — so a write already executing is allowed to finish and be recorded, and the response says
`stopping` rather than `stopped`.

Stopping between a proposal and its approval costs nothing, and that is the common case: the
proposal is not a running write, it is a row. The cancel endpoint is scoped by `user_id` as well as
session, because a run belonging to somebody else is reported exactly as a run that does not exist.

## Failure modes this design accepts

Stated rather than hidden, because each has been considered and each is a real cost:

- **A proposal nobody decides.** It expires. The agent does not chase the user, and there is no
  notification path — a real deployment would want one, and the `expires_at` index is what a sweeper
  would use to find them.
- **A decision during execution.** The repository claims the row for execution before calling the
  backend, so a second decision at that moment is refused with a 409 rather than executing twice.
- **A crash between the decision and the call.** The row is `approved` and unexecuted. Nothing
  retries it automatically, which is deliberate: an automatic retry of a write whose outcome is
  unknown is the failure the ledger exists to prevent. It would need an operator or a subsequent
  decision.
- **Memory of the proposal is not shared with another user.** `user_id` scoping means an assistant
  cannot approve a colleague's write, which is the intent — and also means an out-of-office
  colleague's proposal simply expires.
