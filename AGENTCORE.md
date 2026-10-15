# Running this on AWS with Amazon Bedrock AgentCore

This is the design for moving what is in this repository onto Amazon Bedrock AgentCore. Nothing here is
deployed; the assignment says not to, and an undeployed design that claims to have been tested is worse
than one that says what it has not checked. So the shape of this document is: what I would use and why,
what I would keep and change from the local version, where I expect it to be hard, and — in the last
section, which is the most useful one — **what I would verify before relying on any of it**.

It is written against the AgentCore developer guide as it stands today
([What is AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html)),
which lists Harness, Runtime, Memory, Gateway, Identity, Code Interpreter, Browser, Observability,
Payments, Evaluations, Optimization, Policy and Registry. Everything attributed to a service below is
from that page; everything attributed to *this repository* is from the code here.

---

## The starting point: the contract is already Runtime's

The most useful fact about this build, for this document, is that the agent was written to the AgentCore
Runtime contract from the first commit, because `CONTRACT.md` said so.

| Runtime expects | This repository |
|---|---|
| Port 8080 on `0.0.0.0` | `docker-compose.yml` publishes `8080`, and the service binds `0.0.0.0` |
| `POST /invocations`, JSON in and out | `services/agent/src/agent_service/api/app.py`, shapes in `api/models.py` |
| `GET /ping` returning `{"status":"Healthy"}`, or `HealthyBusy` under load | `HealthResponse`, with `HealthyBusy` reported while a turn is in flight |
| Session id in `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` | Mapped onto `session_id` in `app.py`, so a header client and a body client behave identically |
| An ARM64 image | Both images cross-build for `linux/arm64`; a *run* on ARM64 is not claimed |

So the first deployment step is genuinely "push the image", not "rework the service". That is rare enough
to be worth stating plainly, and it means the interesting parts of this document are the services *around*
the agent, not the agent itself.

---

## The mapping

```
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │  Amazon Bedrock AgentCore                                                    │
   │                                                                              │
   │   ┌────────────────────────────┐        ┌───────────────────────────────┐     │
   │   │ Runtime                    │        │ Memory                        │     │
   │   │  the agent container, as   │◀──────▶│  short-term: the session      │     │
   │   │  built — /invocations,     │        │  long-term: "this user's      │     │
   │   │  /ping, session isolation  │        │  team is Platform", per actor │     │
   │   └───────────┬────────────────┘        └───────────────────────────────┘     │
   │               │ MCP                                                          │
   │               ▼                                                              │
   │   ┌────────────────────────────┐        ┌───────────────────────────────┐     │
   │   │ Gateway                    │◀──────▶│ Identity                      │     │
   │   │  the ticketing MCP server  │        │  the mock's API key becomes   │     │
   │   │  as an MCP target; Policy  │        │  an outbound credential; the  │     │
   │   │  rules intercept tool calls│        │  caller's identity replaces   │     │
   │   └───────────┬────────────────┘        │  a hand-passed user_id        │     │
   │               │                         └───────────────────────────────┘     │
   │               ▼                                                              │
   │        the ticketing mock (unchanged — it is the thing being survived)       │
   │                                                                              │
   │   ┌────────────────────────────┐        ┌───────────────────────────────┐     │
   │   │ Observability              │        │ Evaluations                   │     │
   │   │  OTEL traces + CloudWatch; │        │  the 22 cases, re-run against │     │
   │   │  replaces the JSONL tracer │        │  sessions and traces          │     │
   │   └────────────────────────────┘        └───────────────────────────────┘     │
   │                                                                              │
   │   Not used, and why: Harness (would replace the loop I need to control),     │
   │   Code Interpreter and Browser (no code or web tasks), Payments (no          │
   │   microtransactions), Optimization and Registry (nothing to tune or          │
   │   publish yet)                                                               │
   └──────────────────────────────────────────────────────────────────────────────┘
```

### Runtime — the agent, moved

The container goes up as-is: same image, same port, same two routes. What Runtime adds over
`docker compose up` is the part I could not build locally and would not try to — a session-isolated
execution environment, scaling, and the operational surface around it.

Two things about my container fit Runtime better than the local stack does. It is **stateless between
requests**: the conversation, the approvals and the memory are all in a database, not in process memory,
which is what a runtime that may route two turns of one conversation to different instances requires. And
it **registers nothing durable in process memory** — the one piece of in-memory state, the in-flight-run
registry (`agent/inflight.py`), is inherently process-local and exists only to cancel a turn that *this*
process is running. On Runtime that registry is still correct; it is just smaller, because a cancel only
ever reaches the instance actually running the turn, which is what it is for.

The third thing Runtime changes is one I would have to give something up for. The local stack runs three
containers under one `docker compose up`, with the MCP server as a **sidecar whose image I control**. On
Runtime the agent is the workload and everything it talks to is a service, so the MCP server either
becomes a Gateway target (below) or a second Runtime workload plus a network policy between them. Either
is fine; the point is that the composition stops being mine.

### Gateway — the MCP server, and a policy layer in front of it

Today the agent reaches the mock through `mcp-ticketing`, a container I wrote, over MCP over streamable
HTTP. Gateway solves the same problem — "make these tools available to agents" — with two differences that
matter here.

The first is that Gateway is where **Policy** intercepts. The documentation describes Policy as
deterministic rules (natural language or Dogwood, compatible with Cedar) that "intercept every tool call
before execution", integrated with Gateway. That is precisely the seam `tools/registry.py` sits in
locally, where write tools are recognised and turned into proposals. So the layering I would build is:

- **Policy as a hard, out-of-process floor.** `create_ticket` is not callable by anything without an
  approval token, whatever the model says and whatever prompt injection is attempted. This is defence in
  depth, not the mechanism.
- **The approval gate stays in my code**, because Policy is rules and my requirement is a *human decision
  delivered later*, with the decision recorded, expiring, and surviving a restart. A static rule cannot
  say "wait for Priya to click Approve". Cedar-style policies can express "deny unless this request
  carries an approval claim"; they cannot themselves obtain one.

The second difference is that Gateway's MCP targets would let me delete code. `mcp-ticketing` exists
because something had to own the credential, the retry policy, the error taxonomy and the idempotency
ledger. On AgentCore, a Gateway MCP target plus Identity's outbound credentials could own the first two.
Whether it owns the last two is the interesting question, and it is in the verification list rather than
asserted here: if Gateway retries a call, my ledger has to be *inside* that retry loop, or the retry has
to be idempotent at the mock, and I would not assume either without testing it.

### Memory — long-term facts, and the argument for using it

My memory implementation is deliberately small and the code says so. `memory.extract` is a set of regexes
over the user's own words that pull out a team, a name or a preferred category; `remember` stores them
under a `user_id`; and a test asserts one user's facts are invisible to another. It is about eighty lines
and it is the weakest part of the build in absolute terms — it extracts what it was written to extract and
nothing else.

AgentCore Memory is the clear replacement, and the reason is not that it is more capable in general but
that it models the two lifetimes separately, which is exactly the distinction the assignment asks about
(*what is remembered, where, for how long, and for whom*):

- **Short-term** memory maps onto my `Message` history: turn-by-turn context, scoped to a session,
  expired when the session is.
- **Long-term** memory maps onto my facts: extracted, persisted across sessions, scoped by actor — the
  same role my `user_id` is playing, and the reason I would re-check the isolation story rather than
  assume my schema's guarantee transfers.

Two things I would be careful about, both in the verification list: **consistency** (my facts are read in
the same request that writes them, and if long-term memory is eventually consistent that test needs
rewriting rather than failing in production) and **deletion** (a user-scoped fact store has to be able to
forget, and "forget this" is a request a helpdesk user will make).

### Identity — two credentials I currently hold myself

In the local build there are two places a credential lives, and both are deliberately awkward:

1. **The MCP server's API key to the mock.** `mcp-ticketing` is the only component that holds it, which is
   the reason the MCP boundary exists at all: the agent cannot leak what it never has.
2. **The caller's identity.** Every user-scoped read takes a `user_id` from the request body, and every
   repository method requires one.

AgentCore Identity replaces the first with a managed outbound credential and the second with the caller's
authenticated identity. The second is a genuine improvement I cannot fully make locally: `user_id` in a
JSON body is a claim, not an authentication, and a client that lies about it sees another user's memory.
My tests pin the *isolation* — one user never sees another's facts — but not the *provenance* of the id.
The chat's login makes the name chosen rather than guessed, which is better and still not verification.
On Runtime, a workload identity makes isolation an authentication property instead of a convention, and I
would delete the trust I currently place in a request field. I would want to confirm the identity actually
reaches the container in a form I can attribute a request to, which is in the checklist.

### Observability — the same traces, in the standard format

What I trace locally is the set of things a reviewer of a bad run needs, and this paragraph was
**corrected against the code rather than written from intention** — an earlier draft claimed per-model-call
and retry spans the service did not emit. What it emits now, checkable in `observability/spans.py` and its
call sites: one trace per turn, with a span around the invocation; **a span per tool call carrying its
arguments, its outcome and its duration**; an event when a write is proposed; events when a fact is
recalled or remembered; and an `execution` span on the approval decision, which is a *separate request* and
therefore a separate trace. Token counts are carried through from the provider whenever it reports them —
the deterministic provider used by the evals reports none, so its rows read zero, which `tracer.py` states
plainly rather than leaving a reader to wonder.

Two things are deliberately *not* recorded, and naming them is the point of this paragraph: there is one
span per invocation rather than one per provider round trip, and retries are not spanned at all — they
happen inside the MCP server, which returns the final outcome to a caller that never sees the intermediate
attempts. Both are in the verification list below rather than dressed up as features.

AgentCore Observability is the replacement and the format is the *point*: it ingests OpenTelemetry, which
is what my span model should have been emitting all along. The change is mechanical in the good case —
occupy a real OTEL SDK and keep the span names and attributes — and it buys three things the local tracer
cannot: a UI that does not require reading JSONL, retention and query across many runs, and correlation
with everything else in the account. It is backed by CloudWatch, which is also the main thing to check
(cost and retention, in the checklist).

### Evaluations — my eval suite, pointed at production traces

`eval/` runs 22 cases against the real HTTP contract and exits non-zero if any fails; the cases cover
which tool was chosen, that the approval step is respected, that an out-of-scope request is refused, and a
run with failures switched on. That is a *behavioural* suite, and it is offline: it drives the service from
outside.

AgentCore Evaluations is the natural home for it and adds the mode I do not have — online scoring of real
sessions and traces. Two caveats, both in the checklist: the documentation says it evaluates sessions and
traces from **Strands Agents or LangGraph instrumented with OpenTelemetry or OpenInference**, and my agent
is plain code with a custom span model, so using it means first emitting standard telemetry (which the
Observability change does anyway). And an online evaluator scores what actually happened; the 22 committed
cases assert what *should* happen, and I would keep them as the regression gate regardless of what the
online service reports.

### Harness — considered, and not used

Harness is "a managed agent loop … specify a model, system prompt and tools inline", with orchestration,
tool execution and memory management handled for me. It is the most tempting service on the list for
exactly that reason, and I would not use it, for one specific reason: my agent's core behaviour is
**stopping before a write and waiting for a human**. That decision lives in the loop — it is the loop that
recognises a write tool and returns a proposal instead of calling it — and a managed loop that owns
orchestration owns the place I need to intervene. The trade might go the other way for an agent whose value
is in its prompts and tools rather than its control flow.

### The rest

**Code Interpreter** and **Browser** are not used: this agent reads tickets and creates one, and neither
task involves executing code or driving a web page. **Payments** is not used (no paid APIs).
**Optimization** and **Registry** are the two I would expect to want later rather than never —
Optimization tunes prompts and tool descriptions against traces, which is a real lever for exactly the
weakness my memory extraction has, and Registry is how an organisation would publish `mcp-ticketing` so
other teams' agents could use it — but neither answers a question this build currently has.

---

## What I would keep

### 1. The contract, unchanged

The single most valuable thing here is that the service was written to the platform's contract before the
platform existed in the build. Nothing in the Runtime move touches `/invocations` or `/ping`. Keeping that
discipline — the contract is the platform's, the body is mine — is what makes this document short, and I
would keep guarding it: `tests/contract/test_agent_contract.py` asserts the shapes, and a field added to
the response without a reason is a field the platform now owns.

### 2. The approval gate, in my code, backed by a durable row

The gate is not a prompt instruction and it is not a policy rule; it is a state machine with a database
row behind it. `agent/gate.py` and `agent/execution.py` own the transitions, and the `approvals` table is
what makes "a pending approval survives a restart" true — there is a test that restarts the service between
proposing and approving, and an eval case that does the same over HTTP. That property is the one I would
least like to lose, because everything else about the write path is replaceable and this is not: it is the
difference between an agent that asks and an agent that claims to have asked.

On Runtime this stays in the container and the row moves to a managed database. The mechanism is the same
shape with a different clock: a proposal that expires after 30 minutes is a row with an expiry, so its
lifetime is my decision and not the runtime's session lifetime. I would keep the decision endpoint out of
the agent's critical path for the same reason — the *decision* arrives as a later request, which is exactly
how the platform's request/response model wants it.

### 3. The MCP boundary, even though Gateway could absorb it

Gateway could front the mock directly and the agent would never know. I would keep `mcp-ticketing` as the
thing the agent talks to, for two reasons that are about ownership rather than topology. The error taxonomy
is mine — six kinds of backend failure, each with a message written for a model to act on
(`services/mcp-ticketing/src/ticketing_mcp/errors.py`) — and that is the difference between a model that
retries uselessly and one that tells the user the truth. And the idempotency ledger is mine: `create_ticket`
is guarded by a key minted at *approval-proposal* time, recorded before the call and checked after. Both of
those are behaviour, not plumbing, and moving them into a managed layer means losing the ability to test
them the way they are tested now.

### 4. The deterministic provider

`AGENT_MODEL_PROVIDER=scripted` runs the whole system, including the 22 eval cases, with no API key and no
network. It is the reason the evals are a regression gate rather than a cost centre, and the reason every
screenshot in `docs/media/` is reproducible. Nothing about AgentCore changes that, and I would keep a
scripted provider in the deployed build for exactly this purpose.

### 5. The front end, as a separate client

**The agent serves no page — not locally and not here.** It used to serve one from a Python constant at
`/`, and that is gone: the container is the API and nothing else, and the front end is the Chainlit app in
`services/chat/`, an ordinary client of `/invocations`. This makes the AgentCore story smaller rather than
larger. A runtime endpoint is built to serve a contract, and a question that used to be open — is this
endpoint meant to render HTML for a human? — no longer has to be answered, because nothing here wants to
render anything.

The cost is a deployment I would have to make: the chat is a process of its own, so on AWS it needs a home
rather than a `uv run` on my laptop. App Runner or a small ECS service in front of the runtime endpoint is
the least machinery; a static build served from S3 + CloudFront would need the app to talk to the runtime
from the browser, which means CORS on the runtime and a token in the page, and is the wrong trade for a
chat that holds no state. Either way it is a client deployment and not an agent change — that is the point
of removing the page rather than moving it.

---

## What I would change

| Local | On AgentCore | Why |
|---|---|---|
| SQLite in a Docker volume | Aurora Serverless v2 or DynamoDB | two instances must see one approvals table; a volume is per-container, and the row *is* the durability guarantee |
| Alembic migrations run by `docker-entrypoint.sh` | a deploy step, not an entrypoint step | a runtime that starts instances on demand would race N instances running `alembic upgrade head` at once |
| JSONL traces on disk | OTEL to AgentCore Observability | standard format, a UI, retention, and cross-service correlation |
| `X-API-Key: dev-key-123` to the mock | Identity outbound credential, or Secrets Manager | the key stops being a string in a compose file and becomes rotatable without a redeploy |
| `user_id` in the request body | the caller's identity from Identity | provenance instead of a claim — the one change that strengthens an existing guarantee rather than just tidying |
| local memory regexes | AgentCore Memory | long-term facts with a real extraction and a real lifetime, scoped per actor |
| `docker compose`, three containers | Runtime + Gateway targets (+ the mock somewhere) | the composition becomes managed; see the Runtime section for what that costs |
| both images built `amd64` locally | `linux/arm64` on Runtime | already cross-built; the *run* on ARM64 is the stated limit |

---

## Where I expect it to get hard

Ordered by how much I expect them to cost, not by how interesting they are.

**1. The approval lifetime versus the session lifetime.** My gate assumes an approval can outlive the
process that created it and the session it was created in — a proposal made on Tuesday may be approved on
Wednesday by a different instance. Runtime's session isolation is about execution environments, not about
rows, so the two are compatible; but it means the approval table is *infrastructure*, not a cache, and it
must be reachable from every instance with the same consistency. Everything else in the agent can be
eventually consistent. This cannot: a decision that reads a stale row either executes a write twice or
refuses one that was approved.

**2. Retry semantics inside somebody else's retry loop.** The no-double-write guarantee is currently
proved by a test that makes the backend fail *after* accepting a write and then asserts the ticket count.
If Gateway retries a tool call, there are now two retry loops, and the ledger and the outer loop must agree
about what identifies "the same write". This is the part of the move I would expect to discover a real bug
in, and it is the first thing I would test rather than read.

**3. The blocking-human pattern under a request timeout.** My turn has a 90-second deadline and the
proposal ends the request, which fits a request/response runtime. But "wait for a human" is intrinsically
longer than any request, so the design must never hold a request open waiting — the approval has to arrive
as a later call. Mine does, via a pending approval returned in the response body plus a separate decision
call. What I would check is whether that decision call is best expressed as `/invocations` (the contract's
own suggestion: the user's answer is the next invocation) or as a Gateway-exposed endpoint. Today it is a
dedicated route, `POST /invocations/approvals/{id}/decide`, which is *not* part of the Runtime contract and
would need to become either an invocation or an API Gateway route.

**4. Where the idempotency key comes from when the identity changes.** The key is derived from the approval
today. If Identity owns the write credential, the natural key becomes (actor, tool, arguments) — which is
better, because it survives the approval row being recreated, and worse, because it depends on
canonicalising arguments. Either way it is a decision I would have to make explicitly rather than inherit.

**5. Cold start against a 90-second turn budget.** The budget exists to bound a stuck turn. If Runtime
cold-starts an instance per conversation, the first turn spends part of that budget on startup, and a tight
budget turns a normal first turn into a timeout. I would measure the cold start before choosing the number,
rather than inheriting 90 seconds because it was right on my laptop.

**6. Cost, which I have not modelled at all.** Consumption pricing across Runtime hours, Memory reads and
writes, Gateway invocations, Observability ingestion and CloudWatch storage is a real line item, and this
design has not estimated it. The honest statement is that I would build the smallest possible version and
look at a bill before designing further.

---

## What I would verify before relying on it

I have not deployed this, so every claim above about AgentCore is from the documentation rather than from
experience. These are the things I would check *before* committing to the design, roughly in the order I
would check them, with what I would accept as evidence. Several would change the design rather than just
the configuration.

| # | Check | Why it could change the design | Evidence I would want |
|---|---|---|---|
| 1 | **Runtime's session model and request lifetime.** How long a single `/invocations` request may run, whether the platform streams a response, and what happens to a request when an instance is replaced. | My turn can take up to 90 s and my cancel feature needs a request that is *in flight*. If requests are short-lived or non-streaming, cancellation as I built it has nowhere to live. | the documented limits, plus a deployed turn that exceeds a few seconds and a cancel that lands |
| 2 | **Whether Runtime's expectations match the local contract exactly.** Content type, `HealthyBusy` semantics, whether `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` is the current header name, and whether anything else is required (an initialisation call, a health cadence). | `CONTRACT.md` came from the assignment and is the best guide I have, but it is a summary. A missed required header is a deployment that fails on the first request. | the current Runtime reference, checked line by line against `api/app.py` |
| 3 | **Durability and consistency of the approval table on the target database.** Read-your-writes, and whether a row written by instance A is visible to instance B in the same second. | the single guarantee the design rests on. If the database is eventually consistent, the gate needs a different mechanism — compare-and-set on the decision, not read-then-write. | a two-instance test: propose on one, decide on the other, assert exactly one execution |
| 4 | **Gateway's retry and timeout behaviour, and whether calls can be made idempotent through it.** | two retry loops with different notions of "the same call" is how a duplicate ticket happens. Highest-risk item for a real bug. | Gateway's documented retry policy, then the failure-injection test: fail after accepting, retry, count tickets |
| 5 | **Policy's decision model — static deny, or can it require an approval claim?** And can it see tool arguments? | if Policy can only deny, it is a floor and my gate stays the mechanism (what I assume). If it can require a claim, the layering is stronger and the gate verifies rather than enforces. | Policy's reference: the rule grammar and what it can inspect at interception time |
| 6 | **Identity: what reaches the container, and outbound auth to a non-AWS API.** | if the caller's identity does not arrive in a field I can attribute a request to, my `user_id` remains a body field and isolation stays a convention. If Identity cannot hold a plain API key for the mock, that key stays in Secrets Manager — a smaller change, not a blocker. | docs plus a deployed call showing the identity in the request |
| 7 | **Memory: consistency after write, actor scoping, and deletion.** | my `memory_across_sessions` case reads what it just wrote; eventual consistency would turn a passing test into a flaky one. Deletion is a user-facing promise ("forget that"). | a read-after-write test across sessions for one actor, and a second actor who must not see it |
| 8 | **Observability: OTEL ingestion, what it costs, and whether my span names and attributes survive.** | cost is unbudgeted, and span cardinality is the usual way an ingestion bill surprises people. | a deployed trace visible in the console, and the pricing model for ingestion and retention |
| 9 | **Evaluations' required instrumentation for a plain-code agent.** The docs name Strands and LangGraph with OTEL or OpenInference; mine is neither framework. | if it only scores those frameworks' own spans, using it means adopting a framework, not adding instrumentation — a much larger change, and one I would decline. | a deployed session from a non-framework agent scored, or a clear statement of the required span shape |
| 10 | **Cold start time for my image.** | it eats into the 90-second turn budget, and the image carries a Python runtime, an MCP client and a database driver. | seconds-to-first-request, measured rather than assumed |
| 11 | **Where the chat runs, and what the runtime endpoint requires of a browser-facing client.** | the UI is a separate process, so it needs a host (App Runner or a small ECS service) and the endpoint has to accept its calls — CORS and auth for a browser client are the two things that would decide the shape. | a deployed chat talking to a deployed agent, or a statement of what the endpoint requires of a client |
| 12 | **Region availability and quotas.** Which services are in the region I would use, and the default concurrency and request-rate limits. | a design that depends on a service not in the target region is not a design. | the supported-regions list and the service quotas for the account |

Two of these I would treat as go/no-go rather than tuning: **#3** — if the approvals table cannot be
strongly consistent, the mechanism changes — and **#4** — if a write can be duplicated across two retry
loops, the "never make the same write twice" requirement is not met by the design as drawn. The rest would
be worked around.

---

## What this document does not claim

- **Nothing here is deployed or measured.** No AWS account was used. Where I have said something is likely
  to work — the Runtime move in particular — that is a reading of a contract and a documentation page, not
  a result.
- **The capability list is a snapshot.** AgentCore is a fast-moving platform; this was written against the
  current developer guide, and the service list will have changed by the time anybody reads it. The
  reliable parts are the shape of the design and the questions in the table above.
- **My own services are described from the code, not from memory.** Everything attributed to this
  repository can be checked: the file is named in almost every case, and the behaviour that matters is
  pinned by a test beside it.
