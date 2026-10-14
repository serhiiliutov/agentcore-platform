# Observability: what is traced, and how a bad run is explained

The assignment asks for two things here: what gets recorded, and how you would use it to debug a bad
run. The second is the harder one, so this document does the walkthrough rather than only listing
fields.

## What a trace is

One invocation produces **one trace**, written as two artefacts:

1. **Spans** — a JSONL file, one JSON object per line, under `TRACE_DIR` (`data/traces/<trace-id>.jsonl`
   in the container, a bind-mounted directory on the host).
2. **A summary row** — in the `traces` table, with the session, the user, the duration, the status,
   the provider and model, the counters, and the path to the file.

Two artefacts rather than one, because they answer different questions and have different shapes.
The row answers *"find me the run"*: it is small, indexed by session, user and status, and cheap to
list. The file answers *"explain this run"*: twenty to two hundred events in order, which is what you
actually read when something has gone wrong.

### The five span kinds

`observability/spans.py` defines a closed vocabulary, so a filter means something and a new kind is a
deliberate change rather than a typo at a call site:

| Kind | Emitted by | Recorded when | What it answers |
|---|---|---|---|
| `model_turn` | `agent/handler.py` | once per invocation, around the whole loop | which provider ran, how many model turns it took, how long the turn was, how many tokens the provider reported |
| `tool_call` | `agent/loop.py` | around each executed read | which tool, with which arguments, what came back, how long |
| `proposal` | `agent/loop.py` | a write is proposed instead of executed | that the turn stopped for a human, what was proposed, and the arguments that will run if it is approved |
| `execution` | `factory.py` | an approval is carried out — **in the decision's own trace** | what ran, and whether the backend accepted it |
| `memory` | `agent/handler.py` | a fact is recalled or remembered | how many facts reached the prompt, and which keys were learned — never the values |

Two details in that table are worth reading twice, because both were wrong in an earlier draft of
this document and the code is what settled them.

**There is one `model_turn` span per *invocation*, not one per provider round trip.** A turn that
takes three model turns to answer produces one span and a `model_turns` count of 3. Splitting it
per round trip would be more precise and is not built; the invocation-level span is what the
`model_turns` counter and the summary row already carry.

**An `execution` span does not appear in the turn's trace.** The decision arrives as a *separate
request* — that is the whole shape of the approval protocol — so it writes its own trace, with its
own row in `traces` and the same `session_id`. Explaining "I approved it and nothing happened"
therefore means reading two traces, not one, and the second one is found by session id rather than
by walking the first.

Each span carries `kind`, `name`, `started_at`, `ended_at`, `duration_ms` and a flat `attributes`
object. The attributes are **scalars only** — `_scalar()` coerces anything else to its `repr` — and
that is a deliberate loss: the file has to be readable by tools that were not written for it, and a
trace that cannot be read with `jq` at 2am has lost the property it exists for. A structure worth
preserving gets its own span, not an attribute.

## The decisions that make the trace trustworthy

Four properties, each of which was chosen against a simpler alternative:

**Tracing never fails an invocation.** Every write is wrapped, and a failure is logged and passed to
a callback rather than raised. The reasoning is not defensive habit: the trace exists to explain a
run, so refusing to complete the run because the explanation could not be recorded inverts the
purpose. The callback exists so the failure is *visible* — swallowing it silently would mean tracing
quietly stopped working and nobody noticed until the moment it was needed.

**Spans are written once, at the end, atomically.** A `.partial` file is written and then moved into
place with `os.replace`, so a reader never sees a half-file that looks like a complete trace. The
cost is losing the spans of a run that dies hard — and that is the correct trade, because a missing
trace is unambiguous while a truncated one is not.

**An open span reports no duration, not zero.** `duration_ms` is `None` until `ended_at` is set.
Zero is a real duration; an unfinished span does not have one, and a trace claiming 0ms for something
still running would send a reader looking at the wrong thing.

**The context manager records failures.** `with tracer.span(...)` sets `failed` and `error` on the
span if the block raises, and the tracer's `__exit__` marks the whole run as an error trace when the
turn crashes. A trace that says `ok` for a run that crashed is worse than no trace at all.

## Summarising it makes it findable

The row written alongside the spans is a **five-line index**, not a copy of the trace. That
distinction is what makes the design affordable: listing recent failures, or listing one user's
activity, or finding the slowest run of the week are queries against a small table, and the heavy
detail stays in files nobody has to read unless a specific run is in question.

There is one deliberate redundancy: `trace_path` is stored *and* derivable from the id. Derived
alone would couple every reader to a filename convention; stored alone would be lost when the row is
written before the file. Both means a row is self-sufficient and the writer does not have to know
where it will put things before it decides.

## How I would debug a bad run

The concrete version, because this is the part that is easy to claim and hard to demonstrate.

**The report:** *"I asked the agent to raise a ticket for the jammed printer. It said it had, but
there is no ticket."*

**1. Find the run.** The session id is in the page's URL field and in every response, so the row is
one query:

```sql
SELECT id, started_at, duration_ms, status, error, trace_path
FROM traces WHERE session_id = ? ORDER BY started_at DESC LIMIT 5;
```

Three rows — the two turns of the conversation and an earlier one. The interesting one is the second,
with `status = ok` and `tool_calls = 0`. That is already informative: the turn *succeeded* and called
nothing. A run that created a ticket and then lost it would have `tool_calls = 1`.

**2. Read the spans in order.** The file opens with `jq -c` and reads top to bottom, which is the
primary access pattern the format was built for:

```
{"kind":"model_turn","name":"invocation","duration_ms":412,"attributes":{"prompt_chars":61}}
{"kind":"proposal","name":"create_ticket","duration_ms":0,
 "attributes":{"approval_id":"2a4c89eb-...","arguments":"{'title': 'jammed printer', ...}",
               "state":"pending"}}
{"kind":"memory","name":"recall_facts","duration_ms":0,"attributes":{"facts":0}}
{"kind":"memory","name":"remember_facts","duration_ms":0,"attributes":{"keys":"team"}}
```

(The `memory` spans are here because the prompt named a team; the values themselves are deliberately
not copied into the trace — the keys are. A trace is the last place a user's name should accumulate.)

The `proposal` span is the answer. The agent did not claim a write had happened — it *proposed* one,
the turn ended `awaiting_approval`, and the ticket was never created because nobody approved it. The
report and the trace disagree about what was said, not about what occurred.

**3. If the proposal had been approved and the ticket was still missing,** this trace is not the one
to read — the decision is a separate request with a trace of its own. Ask for the session's traces
and take the second row:

```sql
SELECT id, started_at, status, tool_calls, trace_path
FROM traces WHERE session_id = ? ORDER BY started_at;
```

The last row has `tool_calls = 1` (the write ran) and its file holds the `execution` span. Then the
question is which of the two possible failures it was, and the span settles it immediately:

- an `execution` span whose `outcome` attribute is `executed` → the write reached the backend, and
  the missing ticket is the *mock's* problem, not the agent's;
- an `execution` span whose `outcome` is `failed`, with the error in the summary row → the backend
  refused it, and the detail says whether retrying could ever help (`Authentication` never can,
  `RateLimited` might have).

**4. If the turn had given up instead,** the trace shows a `tool_call` span for the call that was
made — its arguments, its `outcome`, its duration — and the run ends as a deadline rather than as an
answer. One honest limit: the retries are *not* separate spans, because they happen inside the MCP
server, which returns a single outcome to a caller that never sees the individual attempts. What the
trace shows is therefore one call with a duration that includes every attempt, the `failed` outcome,
and the tool's own sentence — which is where "attempted 4 times and has been given up on" is
written. That is the `05-failure-handled.png` path, and it is where "why did this take forty seconds
and then fail?" is answered without guessing.

**5. And the case the design is actually built to survive:** the user says the ticket exists but the
agent said it failed. The `execution` span, the `approvals` row's `executed_at`, and the mock's own
record are three independent sources, and the ledger's `client_request_id` is what ties them
together. A write whose response was lost shows up as an `Unresolved` error with no second attempt —
and the ticket exists.

### What a trace will not tell you

- **Nothing about the model's reasoning.** There is no chain-of-thought in a trace: the provider's
  output is recorded, and a `reasoning` block, where a provider returns one, is not persisted. A
  question about *why the model chose* a tool is answered by the prompt and the tool descriptions,
  not by the trace.
- **Nothing that happened in a previous process.** The in-flight run registry is deliberately
  process-local, so a cancel that reached no process leaves no trace of having been attempted. That
  is a real gap, and it is the same one named in `approval-state-machine.md`.
- **Nothing about another user's run.** Every read is scoped by `user_id`, including this one, so
  "list the failures" means "list *my* failures" in a request. An operator would need the database
  directly, which is the honest answer rather than pretending the API is an admin console.

## The local format, and why it is not OTEL

A local Langfuse or an OTEL collector would have been more moving parts than the assignment needed,
and the requirement was a trace that works in a container with no extra service: no collector to
start, no agent to configure, nothing that can be up in one environment and down in another. JSONL
plus one indexed row satisfies "explain any run from its trace" with the dependency count of a text
file.

It is also, deliberately, a shape that maps onto OpenTelemetry rather than competing with it — span
kind, name, timestamps, duration, flat attributes, parent implied by order. Moving to it is
occupying a real SDK and keeping the names, which is what the AgentCore design proposes and what
items 8 and 9 of [`AGENTCORE.md`](../../AGENTCORE.md) are about verifying.
