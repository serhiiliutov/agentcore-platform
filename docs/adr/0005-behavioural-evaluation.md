# 0005 — A behavioural eval suite, run against the assembled service

## Status

Accepted. Implemented in Stage 13 (`eval/`, `make eval`).

## Context

Every other test in this repository answers a question about a *part*. The unit tests prove the
approval gate refuses an undecided row; the contract tests prove the response shapes; the
integration tests prove a retry is a second request on the wire. Each is hermetic, fast, and
correct — and none of them would have caught the defect Stage 11 found by hand: an approved write
recorded as `executed` when the backend had refused it. Every unit test passed. The assembled
service reported a ticket that did not exist.

That defect had a shape worth naming, because it is the shape this suite exists for. The pieces
were individually right and the *wiring between them* was wrong: the MCP adapter rendered a
failure as the text `ERROR: ...`, the writer was handed that text where it expected a result, and
nothing in either module was wrong on its own terms. A test that drives a module cannot see two
modules disagreeing about what a value means.

The alternative to testing the assembly is testing nothing about the assembly, and shipping the
one class of defect that survives a green suite. So the decision is to run the real thing — two
real servers, real HTTP, the real MCP subprocess — and assert on what it did.

Two constraints shaped the design, and both come from what has actually gone wrong here:

1. **Assertions must be about behaviour, not structure.** An eval asserting "the handler called
   `search_tickets`" restates the implementation and breaks on the next refactor, which trains
   everybody to update the eval without reading it. The requirement is "the agent looked before it
   answered", and that is what a case says.
2. **A failure must explain itself.** An eval whose output is a red cross says that something broke
   and nothing about what. The suite's reporting is part of the deliverable.

## Decision

**One suite, driving the assembled service over HTTP.** `eval/stack.py` starts the ticketing mock
and the agent as real uvicorn servers on free ports, migrates a throwaway database with the same
`alembic upgrade head` a developer runs, and points the agent's MCP subprocess at the running mock.
Every assertion travels over HTTP. Nothing in the suite imports the agent's internals except to
start it.

**Cases are data, in `cases.json`.** A case is a list of steps (`invoke`, or `decide` on a previous
proposal) plus a list of assertions. Adding a case does not mean touching Python, and a reviewer can
read the whole specification of what is measured without reading the scorer. The scorer implements a
closed set of assertion types and *raises* on an unknown one rather than skipping it: a typo that
quietly asserted nothing would make the suite greener and less true, which is the only failure mode
an eval must never have.

**Cases share one running stack.** A fresh stack per case would be simpler and would hide exactly
the class of defect worth catching — state that leaks between turns. The approval cases write real
tickets and the memory cases accumulate real facts, so a case that only passed from a clean slate
would look correct here and fail in a conversation.

**The backend is the witness.** The strongest claim a case can make about a write is not what the
agent said, but whether the store changed. The runner reads the mock's ticket count before the case,
before the decision, and after — three readings, because "the proposing turn wrote nothing" and "the
approving decision wrote one" are different claims and only the third separates them.
`tool_not_executed` exists for the same reason: a proposed write *is* recorded, deliberately, so
`tool_not_called` is the wrong question to ask about it; the right one is whether it *ran*.

**A failing case exits non-zero.** The suite is a gate, not a report. `make eval` is what CI runs.

**The suite is hermetic across runs.** The ticketing server's idempotency ledger is a SQLite file
whose default path is relative, so by default it outlives the mock, which is in memory and rebuilt
per run. A ledger that remembers a key the mock has never heard of makes the *second* run of the
suite behave differently from the first — the write is answered from the record, no ticket is
created, and a case asserting one ticket fails for a reason with nothing to do with the agent.
`stack.py` therefore points `TICKETING_LEDGER_PATH` at the run's own temporary directory.
Reproducibility is the one property an eval cannot do without.

**The `scripted` provider is the default.** The same commit produces the same result, with no API
key and no network. A suite that needed a model would be run rarely and trusted less.

## Consequences

`make eval` is a fifth gate alongside `fmt`, `lint`, `typecheck` and `test`, and the only one that
can fail while all four pass. It is slower — about fifteen seconds against a live stack — and that
is the price of the only test that sees the wiring.

Seven cases cover the families the assignment names: the right tool for a search and for a named
ticket, the approval step respected on both branches (approved writes once, declined writes
nothing), an out-of-scope request answered without a tool call, and two chaos cases with the mock
failing every request. `failures_do_not_duplicate_a_write` states the exactly-once claim under the
conditions that threaten it.

The suite found real defects on its first run, which is the argument for it:

* The write path decided `replayed` by searching the tool's text for the words "replayed" and
  "recovered". The ticketing server's sentence for the case it matters on is "Ticket TCK-1025
  already existed for this request; nothing was created" — which contains neither — so the flag read
  false and the decision told the caller a fresh ticket had been created while the detail beside it
  said nothing had. It now reads the structured field the MCP layer had been sending all along.
* A refused tool call reached the response as a successful one, because the MCP adapter discarded
  `is_error` when it converted the result to text. The loop had no way to know the call had failed,
  so every call was reported as executed.
* A proposed write was reported as a failed call, because `is_error` was derived as "anything that
  is not `executed`" — which makes a write waiting for a human indistinguishable from one that
  broke.

Two things this does not attempt, deliberately. It does not measure *answer quality*: there is no
model in it, and a scripted provider cannot be asked whether its prose was good. And it does not
drive the web page: the page is covered by its own unit tests and by hand, and a browser-driven eval
would add a third process and a source of flakiness for coverage the HTTP contract already provides.
