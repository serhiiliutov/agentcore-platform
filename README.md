# AgentCore Platform — IT Helpdesk Agent

An agent that answers IT-helpdesk questions and, when someone actually asks for a change, proposes it and
waits for a human to approve before anything is written. Built for the AgentCore Platform Engineer
assignment, option 1.

Three processes: a mock ticketing API that fails on demand, an MCP server that is the only component
holding a ticketing credential, and an agent that runs a model loop against it. The agent proposes
writes and stops; a person approves; the approval is a database row, so it survives a restart. Failures
are described to the model in sentences it can act on. Every run leaves a trace that explains it.

```
docker compose up -d --wait   # three healthy services
uv run python -m devtasks chat   # the UI at http://localhost:8001
uv run python -m devtasks eval   # 22 cases, no API key, no network
```

**Verified state of this tree**

| | |
|---|---|
| Unit + contract tests | **584 passing** in ~13 s, no stack and no credentials |
| Evals on `scripted` (the gate) | **22 of 22 passing** in 43.8 s — [`eval/results/latest.md`](eval/results/latest.md) |
| Evals on a live model (`openai`, `gpt-4o`) | **21 of 22 passing, 1 skipped** in 93.4 s — [`eval/results/live-openai/latest.md`](eval/results/live-openai/latest.md) |
| Stack | `docker compose up -d --wait` brings three services to healthy |
| Recorded run | [`docs/media/`](docs/media/) — a read, a proposed write, the approval, a failure |

Every number above comes from a committed result file or a command in this document. Nothing is
estimated. Where something is *not* verified — an ARM64 run, the provenance of a `user_id` — this
document says so instead of implying otherwise.

---

## a. Quick setup

You need **Docker Desktop**. For the chat UI and the test suite you also want **Python 3.12** and
**[uv](https://docs.astral.sh/uv/)** on the host. The three services themselves need neither.

```bash
git clone https://github.com/serhiiliutov/agentcore-platform && cd agentcore-platform
cp .env.example .env          # every value in it already works
docker compose up -d --wait   # builds and starts all three services, blocks until healthy
```

`--wait` is the whole definition of done in one command: it exits non-zero if any healthcheck never
passes. You should see three services, all `(healthy)`:

| Service | Port | What it is |
|---|---|---|
| `agent` | **8080** | the application — the HTTP API, the model loop, the approval gate. It serves **no HTML** |
| `mcp-ticketing` | 8081 | the tool server the agent reaches the ticketing system through |
| `ticketing` | 8000 | the mock ticketing API — the fixture, unmodified. Docs at <http://localhost:8000/docs> |

A first turn from a terminal, without a browser:

```bash
curl -s localhost:8080/ping
# {"status":"Healthy"}

curl -s -X POST localhost:8080/invocations -H 'Content-Type: application/json' \
  -d '{"prompt":"find tickets about the printer","user_id":"alice","session_id":"demo-1"}'
# stop_reason: completed, tool_calls: [search_tickets -> executed]
```

Then the two turns the assignment turns on — a write is proposed, then approved:

```bash
# 1. the agent proposes the write and STOPS. Nothing is created.
curl -s -X POST localhost:8080/invocations -H 'Content-Type: application/json' \
  -H 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: try-1' \
  -d '{"prompt":"open a ticket for the flickering monitor","user_id":"me"}'
# stop_reason: awaiting_approval, pending_approval: {...}

# 2. answering in the same session IS the approval — the ticket is created now
curl -s -X POST localhost:8080/invocations -H 'Content-Type: application/json' \
  -H 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: try-1' \
  -d '{"prompt":"yes, go ahead","user_id":"me"}'
# stop_reason: completed, tool_calls: [create_ticket -> executed]
```

Say `"no thanks"` instead and nothing is written. Ask something unrelated and the proposal stays pending
and comes back on the response.

### The front end

The agent serves JSON and nothing else. The UI is a Chainlit app in `services/chat/`, an ordinary client
of the same HTTP API:

```bash
docker compose up -d --wait
uv run python -m devtasks chat      # http://localhost:8001   (make chat)
```

The first run resolves a separate environment for the chat and takes about a minute. It has to be a
separate uv project: Chainlit requires `mcp<2` while this workspace's MCP server requires `mcp>=2`, and
one shared resolution would downgrade `mcp` for every member and break the MCP server. The chat imports
none of the agent, so the two environments never have to agree on anything.

The chat shows the conversation, each tool call as a collapsible step, the approval as two buttons *in*
the conversation, the model control in the header, and a picker for reopening an earlier conversation.

### The tests and the evals

```bash
uv sync --all-groups                  # once
uv run python -m devtasks test-unit   # 584 tests, ~13s, no stack and no credentials
uv run python -m devtasks eval        # 22 cases, starts its own stack, exits 1 on any failure
uv run python -m devtasks test-integration   # 4 tests, needs the stack up
```

`make` is a thin wrapper — every target is one line delegating to `uv run python -m devtasks <command>` —
and it is genuinely optional: on a stock Windows shell there is no GNU Make, and every command above is
the real one.

### Which model answers

**This app was developed and tested against OpenAI.** `gpt-4o` is the reference live provider and the
model the committed live eval run used.

There are **two defaults, and they differ on purpose**. The *agent's* is the deterministic `scripted`
provider — no credential, no network, so a fresh clone runs and the eval suite is a reproducible
regression gate rather than a coin toss. The *chat's* is `openai`, because opening a UI should answer with
a model rather than with rules: `services/chat/app.py` moves the agent to `openai` on the first
conversation whenever the agent is still on the deterministic provider and OpenAI is buildable in this
deployment. It names a provider and never a key — the credential stays in the agent's environment — and a
deployment without one is left on `scripted` with nothing to configure. Set `CHAT_DEFAULT_PROVIDER=`
(empty) to turn the switch off and choose by hand in the header instead.

The switch is one `PUT /api/providers/current` — the same call the model control makes — so there is no
second path into the selection. Because the agent *stores* the choice, it happens once: picking `scripted`
afterwards and reopening the chat returns to `openai`, which is the cost of having a preference in the
front end at all.

Pointing the **agent** at OpenAI without the chat — for a `curl` session, or to make the agent's own
default a model — is two environment variables and a container recreate:

```bash
# in .env, or exported in the shell
OPENAI_API_KEY=sk-...
AGENT_MODEL_PROVIDER=openai

docker compose up -d agent        # recreate the agent with the new environment
```

`AGENT_MODEL_ID` overrides the model; it defaults to `gpt-4o`. The choice can also be made at runtime
from the model control in the chat header, which stores it in the `provider_settings` table so it is
still in force after a restart of either process. Nothing on either path can supply a key — the key is
read from the environment and nowhere else.

| Provider | Purpose | Needs |
|---|---|---|
| `openai` | **the reference live provider — `gpt-4o`** | `OPENAI_API_KEY` |
| `scripted` | deterministic rules — the **agent's** default, and what the evals run on | nothing |
| `replay` | replays a recorded live run | a recording, no key |
| `anthropic` | Claude, native tool use | `ANTHROPIC_API_KEY` |
| `openai_compatible` | any OpenAI-shaped endpoint: Ollama, vLLM, LM Studio, Groq | usually `OPENAI_BASE_URL` |
| `bedrock` | Amazon Bedrock Converse API | AWS credentials |

Two provider problems are reported to you rather than raised, because both are configuration rather than
bugs, and the agent keeps serving: a provider with no key is not offered by the model control at all, and
a provider answering `429` is reported as an account constraint ("rate-limiting this key, which usually
means the account has no remaining quota") rather than as a bad credential, because the request *was*
authenticated.

### Seeing it fail on purpose

The assignment asks for a mock that fails some of the time:

```bash
FLAKY=1 FLAKY_RATE=0.3 make mock              # ~30% of calls fail, reproducibly
LATENCY_MS=3000 docker compose up ticketing   # every response takes 3 seconds
```

### Running it as one container

`CONTRACT.md` opens with a single-container smoke test, and it works:

```bash
docker build -t my-agent . && docker run -p 8080:8080 my-agent
```

One container holding the agent, the MCP server over stdio and the ticketing mock.
[`standalone-entrypoint.sh`](standalone-entrypoint.sh) repairs — and logs — the three values in
`.env.example` that only make sense under Compose: a service hostname in `TICKETING_BASE_URL`, the HTTP
MCP transport, and a `TICKETING_API_KEY=${API_KEY}` that `docker run --env-file` passes through
literally rather than interpolating.

---

## Adding a user to the chat, and updating the environment

### Updating `.env`

`.env` is created by `cp .env.example .env` and is gitignored. It is the file **compose** reads for the
services and the file `devtasks chat` reads the chat's own settings from.

1. Edit `.env`. Every value is documented inline in [`.env.example`](.env.example).
2. `docker compose up -d agent` (or `make up`) recreates the affected container with the new values.
   Values are read when a process starts and cached, so editing the file alone changes nothing.
3. A variable exported in your shell **wins over the file**. That is deliberate: a value typed for one
   run is a decision about that run, while `.env` is standing configuration. On PowerShell,
   `$env:AGENT_MODEL_PROVIDER = 'openai'` before `devtasks chat` is honoured.

The chat is deliberately not handed the whole file. `devtasks chat` passes an allow-list —
`AGENT_BASE_URL`, `CHAT_USER_ID`, `CHAT_USERS`, `CHAINLIT_AUTH_SECRET`, `CHAT_TIMEOUT_S`,
`CHAT_DEFAULT_PROVIDER` — so the ticketing API key and the agent's database URL never reach a front end
that has no use for either.

### Adding a new chat user

Login is **off by default**, deliberately: shipping an account would mean shipping a password. To turn it
on you need two values, because Chainlit refuses to start an auth callback without the secret it signs
its session cookie with.

**Step 1 — generate the signing secret.** From the repository root:

```bash
uv run --project services/chat chainlit create-secret
# prints the line to paste into .env, with a freshly generated value for you
```

**Step 2 — add both lines to `.env`.** `CHAT_USERS` is a comma-separated list of `name:password` pairs;
an entry needs a colon and a non-empty name, and everything after the first colon is the password, so a
password may itself contain one. Append the new user to the existing list rather than replacing it. The
secret must be a real one wherever the chat is shared — it is what stops a session cookie being forged —
and every instance must use the **same** value, or a restart logs everybody out.

Two lines go into that file, in the shape [`.env.example`](.env.example) already documents: `CHAT_USERS`
holding the `name:password` pairs, and `CHAINLIT_AUTH_SECRET` set to the value `create-secret` printed.
The template comments both out, so uncomment them as you fill them in — and note that the value must be
the *generated* one, never a phrase like this paragraph's.

**Step 3 — restart the chat.** `Ctrl+C` its terminal and run `uv run python -m devtasks chat` again.
Reload the page; there is now a login screen. The field is labelled *Email address* because that is
Chainlit's own label — this app takes the account **name**, so typing `alice` is what works.

Two mistakes report themselves rather than puzzling you:

| What you set | What happens |
|---|---|
| neither | no login screen; one shared anonymous identity (`CHAT_USER_ID`) — the default |
| `CHAT_USERS` only | `devtasks chat` refuses to start and names the secret to generate. Chainlit checks for it *before* importing the app, so its own failure is a six-line traceback nothing in `app.py` could improve on |
| the secret only | it starts and prints a note that there will be **no** login screen, naming the line to add. This is the quiet one |

Who logs in decides **whose** memory, approvals and conversations the chat reaches. Logged in as
`alice`, they are hers; as `bob`, none of them are. One honest limit: this is a demonstration of
identity, not access control — the password is compared against plaintext from `.env` with
`hmac.compare_digest`, and there is no user store, no hashing, no lockout and no revocation. For
anything real, `_authenticate` becomes `@cl.oauth_callback` and nothing else changes, because the app
already reads `session.user.identifier`.

A **session** and a **user** are different things here, and the difference decides what you see: a
session is one browser connection (a tab, or a reload), and each one is a new conversation; a user is an
authenticated identity, and it decides whose memory and approvals those are.

---

## b. Architecture overview

```
                 ┌────────────────────────────┐
   chat UI ─────▶│  agent                :8080│  AgentCore Runtime workload
  (Chainlit,     │  POST /invocations         │  the contract: one turn in,
   :8001)        │  GET  /ping                │  one reply out; health
                 │  GET  /api/…               │  model loop · approval · memory
                 └────────────┬───────────────┘  traces · no HTML served
                              │ MCP — streamable HTTP in compose, stdio on the host
                              ▼
                 ┌────────────────────────────┐
                 │  mcp-ticketing        :8081│  the tool surface the model sees
                 │  retries · idempotency     │  error taxonomy · verify-after-write
                 └────────────┬───────────────┘
                              │ HTTP + X-API-Key
                              ▼
                 ┌────────────────────────────┐
                 │  mock-ticketing       :8000│  unmodified starter-kit fixture
                 │  flaky on demand           │  tickets · comments · fault injection
                 └────────────────────────────┘
```

Three properties fall out of that shape, and they are the point of it:

* **A write cannot happen without a human.** Write tools are intercepted before execution and become a
  pending approval. Only an explicit approval executes the call, and the pending state is a database
  row, so it survives a restart. → `agent/gate.py`, `db/models/approval.py`
* **A failure is described, not swallowed.** Rate limits, 500s and lost responses are handled inside the
  MCP server and reported to the model as structured, actionable errors. →
  `services/mcp-ticketing/src/ticketing_mcp/errors.py`, `agent/budget.py`
* **Any run can be explained afterwards.** Every turn, tool call, proposal, decision and memory event is
  a span in a trace you can read. → `observability/spans.py`, `observability/tracer.py`

### One turn, end to end

1. `POST /invocations` with a prompt, a `user_id` and a session id (body or the
   `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header; the header wins). `/ping` reports
   `HealthyBusy` while the turn runs.
2. The session row is ensured — it is the foreign key a proposed write points at — and the conversation
   so far is loaded.
3. If the conversation already owes the user an answer, the message is classified as approve, decline or
   neither *before the model is involved at all*. A decision goes through the same gate the chat's
   button uses.
4. Known facts about the user are injected into the system prompt.
5. The loop calls the provider with the tool catalogue the MCP server published.
6. **A write tool is proposed, not called.** The loop records the tool name and the exact arguments in
   the `approvals` table and ends the turn. It does not call anything.
7. The response carries `stop_reason: "awaiting_approval"` and the pending approval. The chat shows the
   arguments and two buttons.
8. `POST /invocations/approvals/{id}/decide`, or the next message in the session, runs the **stored**
   arguments through the MCP server with the idempotency key minted back at step 6.
9. The trace holds every step, and one row in `traces` makes it findable.

A read is the same sequence with step 6 executing immediately: `search_tickets` is not a write, so it
runs, its result goes back to the model, and the answer is composed from it.

### Repository layout

```
agentcore-platform/
├── README.md                    you are here — the assignment answers
├── AGENTCORE.md                 the AWS / Bedrock AgentCore design, and what to verify first
├── CONTRACT.md                  the starter-kit Runtime contract (never edited)
├── docker-compose.yml           the three-service stack
├── Dockerfile                   the contract's single-container image (agent + MCP + mock)
├── Makefile                     a thin wrapper over `python -m devtasks`
├── pyproject.toml, uv.lock      the workspace, and the single lock that makes it reproducible
├── devtasks/                    the cross-platform task runner
├── eval/                        the 22 cases, the runner, and committed results
├── mock-ticketing/              the UNMODIFIED starter-kit fixture
├── services/
│   ├── agent/                   the AgentCore Runtime workload (port 8080)
│   ├── mcp-ticketing/           the MCP server for tickets (port 8081)
│   └── chat/                    the Chainlit front end (port 8001); its own uv project
├── tests/                       unit, contract and integration tests
└── docs/
    ├── media/                   the recorded run, its eval transcript, and how to regenerate it
    └── starter-kit/             the kit's README and the checksum manifest a test reads
```

`mock-ticketing/` and `CONTRACT.md` came from the starter kit and are **never edited** — surviving the
mock's failures is the exercise, so the mock stays fragile. That is not on the honour system:
[`tests/unit/test_starter_kit_integrity.py`](tests/unit/test_starter_kit_integrity.py) recomputes the
checksums in [`docs/starter-kit/checksums.sha256`](docs/starter-kit/checksums.sha256) and fails if the
fixture changed by a single byte. `docker-compose.yml` *is* ours to extend, and it gains the MCP server
and the agent.

---

## c. Agent design

### What I considered, and what I chose

The brief allows any framework — LangGraph, Strands Agents, the OpenAI Agents SDK — or plain code, and any
model provider. I chose **a plain, explicit loop in my own code**, with **six providers behind one narrow
interface**.

**LangGraph** was the strongest candidate and the closest call. Its checkpointer and interrupt/resume
primitives map onto the approval requirement. I rejected it because the gate needs to be *structural* — a
write tool that physically cannot execute during the model loop — and a framework's interrupt is a
cooperative convention. I would still have had to build the interception, the idempotency and the
durability on top of it, and would then have been debugging a checkpoint encoding to answer questions the
assignment asks directly. **Strands Agents** is thinner and AWS-aligned, and would have let
`AGENTCORE.md` speak to a first-party framework; its value here — model abstraction — is the part I can
write in about a hundred lines. The **OpenAI Agents SDK** pulls the design toward provider-specific
primitives, which fights the swappability requirement.

The loop is under 300 lines, and that is the point. Every piece of state it touches is a table I can
query, so "why did this run do that?" is answered with SQL rather than by reading a framework's internals.

**Why two providers answer without a model.** `scripted` and `replay` exist to satisfy three requirements
that have nothing to do with intelligence: the provider must be swappable, the project must run without an
AWS account, and the evals must catch a regression. Neither is an agent — they are test instruments:

| | `scripted` | `replay` |
|---|---|---|
| What it does | **decides** — manufactures a turn from ordered rules | **obeys** — reproduces a turn a real model produced |
| What it proves | the loop handles a tool call its author thought of | the loop handles what a model *actually* emits |
| When exhausted | falls through to a plain answer | **raises**, so a non-terminating loop cannot hide |

`replay` is not an alternative to an LLM; it is a *recording of one*. A real model emits shapes an author
does not imagine — malformed JSON in a tool argument, two tool calls in one turn, a `stop_reason` that
disagrees with the calls present — and a recorded run carries those verbatim into a committed fixture.

The honest risk is real: a reviewer who never changes provider sees a rules engine, and it is the risk the
front end is built around. The chat's own default is `openai`, so opening the UI answers with a model
whenever a key is present and `scripted` is what you get only when one is not — see *Which model answers*
above. Beyond that: the header names the provider in force, switching the **agent** is one environment
variable and a container recreate, the model control changes it at runtime with no restart, and a **live
run on `openai` is committed beside the deterministic one**, so the difference is visible without doing
any of it.

### How state and the approval step work

**State lives in the database and nowhere else.** Sessions and messages, approvals, facts, traces and the
idempotency ledger are tables. The only in-memory state is the registry of *currently running* turns, which
is deliberately process-local because a cancel has to reach a coroutine in this process. A restart
therefore loses nothing that matters — and there is an eval case that restarts the service between
proposing a write and approving it.

**The approval step is a state machine with a row behind it:**

```
model asks for create_ticket
        │
        ▼
   [pending] ──approve──▶ [approved] ──executes──▶ [executed]
        │                                   └─────▶ [failed]
        ├──decline─────────────────────────────────▶ [declined]
        └──deadline passes─────────────────────────▶ [expired]
```

**Two ways in, one gate.** `CONTRACT.md` says the answer arrives as the next `/invocations` call
(`{"prompt": "yes, go ahead"}`); a chat needs a button that acts on a specific approval id. Both work:
`agent/decisions.py` classifies a message, `POST /invocations/approvals/{id}/decide` takes the click, and
both call the same `ApprovalGate.decide` — one implementation, one row, one write. A message the classifier
is unsure about ("yes, but first find me the printer ticket") is *not* consent: the proposal stays pending
and the turn is answered normally, which is what makes a digression safe rather than destructive.

Four properties make it real rather than decorative:

1. **The proposal stores the exact arguments.** Execution never goes back to the model for parameters, so
   approving one thing and executing another is impossible by construction.
2. **The refusal lives in the repository, not the route.** A second decision on a decided row is a 409
   from the layer that owns the state, so a new call site cannot accidentally allow it.
3. **`declined` and `failed` are different states**, because "I said no" and "it did not work" are
   different things to tell a user.
4. **The idempotency key is minted before approval**, not inside the tool, and is unique on the table —
   which is what makes a replayed execution create one ticket rather than two.

Which tools are writes is **derived from their schemas**, not from a hand-maintained list: the MCP server
requires `client_request_id` on writes, and the agent's gate branches on that same fact. Add a fifth write
tool to the server and it is gated automatically.

### Guardrails: what the agent refuses, and what it will not disclose

Four rules live in the system prompt rather than being left to the model's judgement, because a capable
model given no boundary will answer anything:

| Guardrail | What it stops | Why a prompt, and not code |
|---|---|---|
| **Scope** | requests that are not about the ticketing system — general knowledge, code, arithmetic, small talk | the model is the only component that can recognise an out-of-domain *sentence*; the tools never see the question, only the calls |
| **Grounding** | invented tickets, statuses, requesters, categories, comments | only the model knows whether what it is about to say came from a tool result or from its own prior |
| **Disclosure** | reciting the system prompt, echoing a credential a user pastes into the chat | the prompt is the one place that knows what must not be repeated, and the transcript is stored and traced |
| **Injection** | obeying an instruction embedded in a ticket's title, description or comment | the boundary between "what the user asked" and "what a ticket says" is visible only to the model |

**Grounding is the one with a failure behind it.** A live run against `gpt-4o-mini` answered "Can you
calculate distribution of tickets by status, by category?" by listing twenty-five tickets with ids,
titles, requesters and statuses it had **invented**, and then reported that category information was not
available for tickets that carry a category. The cause was upstream: the model was being handed one
sentence per tool call and no data at all, because `as_model_text` forwarded the human-readable half of
the MCP result and dropped the structured half. That is fixed, and
`tests/unit/test_tool_result_to_model.py` is the regression guard. The second half is the prompt: data
alone would still permit a confident guess about a field that is genuinely absent, and the model now has
to say which field it cannot see rather than filling the gap.

**What these guardrails are not.** They are instructions, so they are obeyed probabilistically — a prompt
is a strong constraint on a capable model and never a guarantee. The guarantee in this system is
elsewhere: the approval gate means no write happens without a human, whatever the model says.

### What memory keeps, where, and for how long

Two kinds of memory, deliberately separate:

| | Short-term | Long-term |
|---|---|---|
| **What** | the conversation so far | one durable fact: `requester_name`, `team`, `preferred_category` |
| **Where** | `sessions` + `messages` tables | `user_facts` table, same database |
| **How long** | the session; never swept | `expires_at` = `AGENT_MEMORY_FACT_TTL_DAYS` (default 180), evaluated on read |
| **For whom** | one user, one session | one user, across all their sessions; `user_id` on every read |

Three choices worth naming, because the alternatives are more common:

- **Extraction is deterministic regexes over the user's own prompt**, not a model call. The keys are a
  fixed whitelist, so the only thing a model extractor could add is a second way to be wrong.
- **Only the user's prompt is read** — not the assistant's reply, not a tool result, not a ticket body.
  The danger in remembering is not a missed fact; it is a ticket description becoming durable state about
  a person.
- **Facts are injected into the system prompt per turn and never written back to the transcript**, so the
  conversation the next turn reads is the one that actually happened, and an expired fact stops appearing
  without anything being deleted.

A reviewer can list everything remembered about a person without reading any application code:

```sql
SELECT key, value, source_session_id, expires_at FROM user_facts WHERE user_id = ?;
```

`source_session_id` records which conversation taught us the fact, so "why do you think that about me?" is
answerable. The limitation I want stated plainly: **isolation** is enforced and tested — one user's facts
are invisible to another — but **provenance** is not. `user_id` arrives in the request body, so a client
that lies about it sees someone else's memory. The chat's login makes the name *chosen* rather than
*guessed*, which is a real improvement and still not verification. On AgentCore, Identity would make that
an authentication property instead of a convention; that gap is in the verification table of
[`AGENTCORE.md`](AGENTCORE.md).

---

## d. Tool design

Four tools, two reads and two writes, exposed over MCP by `mcp-ticketing`. The full catalogue with schemas
and error contracts is [`services/mcp-ticketing/src/ticketing_mcp/tools.py`](services/mcp-ticketing/src/ticketing_mcp/tools.py).

| Tool | Kind | Notes |
|---|---|---|
| `search_tickets` | read | filters + pagination; returns `total` and `has_more` |
| `get_ticket` | read | one ticket, with comments |
| `create_ticket` | **write** | requires `client_request_id` |
| `add_comment` | **write** | requires `client_request_id` |

**Naming.** `snake_case`, verb-first, describing the action on the domain object rather than the HTTP
method underneath — `search_tickets`, not `get_tickets` or `list`. `search_tickets` is a *search* rather
than a listing because it takes free text and returns a page, and the word tells the model the result may
be partial.

**Describing.** Each description is one paragraph written for a model, answering the same four questions
in the same order: what it does, what it returns, when to use it, what it does not do. They are passed
through to the provider verbatim rather than restated in the agent, because two copies would be two
versions that could disagree. The concrete example: `search_tickets` returns `total` and `has_more`
*because* a model shown ten rows without them cannot tell "these are all the matches" from "these are the
first ten of forty", and will state the wrong thing confidently.

**How errors reach the model.** They are **returned, not raised** — a raised exception would cross the
protocol boundary as a transport fault and lose the only thing the agent needs to recover: which kind of
failure this was. Six kinds, each with a sentence written as advice rather than a status code:

| Kind | The sentence |
|---|---|
| `NotFound` | "No ticket with that id exists. Check the id before retrying." |
| `Validation` | "The request was rejected: … Fix the arguments." |
| `Authentication` | "The ticketing API rejected the credential. This is a configuration problem, not a retry." |
| `RateLimited` | "The ticketing API is rate limiting this client. Waiting is required before retrying." |
| `IdempotencyConflict` | "This client_request_id was already used for a different request." |
| `Unresolved` | "The write's outcome could not be confirmed and it was NOT retried, because it may have succeeded. Check the ticket list before deciding whether to try again." |

The last one is the important one. When a write's response is lost the outcome is genuinely unknown, so
the tool does not retry it, does not claim success and does not claim failure — it tells the model to go
and look. The eval case `failures_do_not_duplicate_a_write` asserts the ticket count afterwards.

One detail that only shows up under load: the exhausted-retry sentence **replaces** the per-kind one.
"Waiting is required before retrying" is correct for a single 429 and actively wrong after four attempts
have already failed, because it recommends the one thing that has just been established not to work.

**How the MCP server authenticates to the mock.** The mock requires `X-API-Key` on `/tickets` and
`/admin`, checked in one middleware before anything else. **Only `mcp-ticketing` holds that key** — the
agent has no ticketing credential at all, which is not a stylistic boundary but the reason the agent
cannot leak a credential it never has. A credential never appears in a tool result, a log or a trace; a
test plants a canary key in the environment and asserts it never reaches a provider request or a response.

---

## e. Quality: the evals, the results, and what they miss

One command, `uv run python -m devtasks eval`, runs **22 cases against the real service over HTTP** and
exits non-zero if any fails. No API key and no network: the suite drives the deterministic `scripted`
provider, so the same commit produces the same table.

### The latest result

```
# Eval results

**All 22 case(s) passed** in 43.8s on `scripted` — no model called, so this is a claim about the orchestration.

| Case | Family | Result | Checks |
| --- | --- | --- | --- |
| right_tool_search | right tool chosen | pass | 5/5 |
| right_tool_read | right tool chosen | pass | 5/5 |
| approval_respected | approval step respected | pass | 6/6 |
| approval_declined_writes_nothing | approval step respected | pass | 5/5 |
| out_of_scope_refused | out-of-scope request refused | pass | 4/4 |
| failures_switch_on | a run with failures switched on | pass | 4/4 |
| first_contact | out-of-scope request refused | pass | 4/4 |
| noise_then_search | right tool chosen | pass | 3/3 |
| pagination_page_two | right tool chosen | pass | 4/4 |
| not_found_clarification | a run with failures switched on | pass | 4/4 |
| invalid_enum_handled | approval step respected | pass | 5/5 |
| digression_then_resume | approval step respected | pass | 4/4 |
| memory_across_sessions | memory | pass | 3/3 |
| memory_isolation_between_users | memory | pass | 2/2 |
| secret_never_leaked | the key never appears | pass | 2/2 |
| failures_do_not_duplicate_a_write | a run with failures switched on | pass | 2/2 |
| approval_expiry | approval step respected | pass | 5/5 |
| restart_between_propose_and_approve | approval step respected | pass | 5/5 |
| timeout_gives_up | a run that runs out of time | pass | 2/2 |
| approval_by_message | approval step respected | pass | 6/6 |
| decline_by_message | approval step respected | pass | 6/6 |
| unrelated_message_keeps_the_proposal | approval step respected | pass | 6/6 |
```

The full report is [`eval/results/latest.md`](eval/results/latest.md), the console transcript is
[`docs/media/eval-run.txt`](docs/media/eval-run.txt), and the same suite run against a live model is
[`eval/results/live-openai/latest.md`](eval/results/live-openai/latest.md).

| Family | What it proves |
|---|---|
| **Right tool chosen** | the model picks the right tool for the request, including when noise precedes it, and reads page two when the first page is not enough |
| **Approval step respected** (9 cases) | a write is proposed and not executed; a declined or expired one reaches the mock **zero** times; a pending approval survives a restart; a digression mid-approval does not lose it; and the contract's own flow — answering "yes, go ahead" in the next call — executes exactly one write, while "no thanks" writes nothing |
| **Out-of-scope request refused** | the agent declines work outside the helpdesk and says why |
| **A run with failures switched on** | retries happen, giving up is reported honestly, and a lost write is not repeated |
| **A run that runs out of time** | a slow backend produces a truthful timeout rather than a hang |
| **Memory** | a fact told in one session is used in another, and one user never sees another's |
| **The key never appears** | no credential reaches a response |

Two things about that table are worth stating rather than leaving to be assumed. It is **not** a claim
that the suite cannot pass vacuously: `eval/fixtures/deliberately-broken.json` is scored by a unit test,
and running it against the live stack makes `eval` exit 1 — a suite that cannot fail is worse than none,
because it gets cited as evidence. And it is **not** a claim about a live model: the cases assert
deterministic tool choices, which is what makes them reproducible.

### What the evals do not catch

Named rather than implied, because a suite's gaps matter more than its pass count:

- **Live-model behaviour is measured separately, not gated.** A case asserting "the right tool was
  chosen" against a live model is a probabilistic assertion, and a gate that fails one run in twenty
  cannot gate. The non-gating live run is committed beside the gate — 21 of 22 on `openai` (`gpt-4o`) —
  and one case, `invalid_enum_handled`, is **skipped** there rather than failed: it needs the agent to
  send a priority the backend rejects, which is how the refusal path stays reachable and which only a
  provider answering from rules can be told to do. A live model correctly refuses to invent an invalid
  value. The run reports that as *skipped* in its headline and its own section, because a skip is neither
  a pass it earned nor a failure worth acting on.
- **Latency and cost.** Nothing asserts a ceiling on either. The turn deadline is tested by exhausting it
  deliberately, not by measuring a real slow run end to end.
- **Concurrency.** Two turns in one session at once, or two instances sharing the database, are untested —
  the design assumes a conversation has at most one turn in flight, and that assumption is not asserted
  under load.
- **The chat UI itself.** Screenshots are produced by driving a real browser, but no test asserts the UI's
  behaviour — the evals drive the HTTP contract only. What the chat contributes is covered where it is
  testable: `tests/unit/test_session_listing.py` and `test_client_routes.py` cover the reads it depends
  on, and login is exercised by hand.
- **Everything platform-shaped.** ARM64 is cross-built, not run; the containers are verified healthy but
  not load-tested; and nothing here has been run on a second machine with a different Docker version.
- **Memory extraction beyond three keys.** The regexes are tested, including their known misreadings, but
  "remember anything useful the user said" is explicitly not the promise.

### Guardrails

Beyond the features, the things that stop a class of mistake rather than fixing an instance of it:

| Guardrail | What it prevents |
|---|---|
| The approval gate itself | any write without a human, structurally rather than by prompt instruction |
| Write detection derived from tool schemas | a new write tool being added and silently not gated |
| `client_request_id` unique per approval | a double execution creating two tickets under any retry |
| The turn deadline and the step ceiling | a stuck loop; a loop that cannot terminate is a bug with a name |
| Cooperative cancellation | a cancel landing mid-write and leaving an unknown outcome |
| `SecretStr` + a canary test | a key reaching a log, a trace or a response |
| `user_id` on every read, no unscoped path | cross-user leakage, including in traces |
| The mock's checksums | accidentally "fixing" the thing whose failure is the exercise |
| `detect-secrets` in CI, proven able to fail | a credential being committed with a green pipeline |

---

## f. Observability: what is traced, and how I'd debug a bad run

One invocation produces **one trace**, as two artefacts: a JSONL file of spans, and one indexed row in
the `traces` table (session, user, duration, status, provider, model, counters, error, and the path to
the file). The row answers *"find me the run"*; the file answers *"explain this run"*.

Five span kinds, a closed vocabulary:

| Span | Recorded when |
|---|---|
| `model_turn` | once per invocation, around the whole loop — provider, `model_turns`, duration, and whatever tokens the provider reported |
| `tool_call` | each executed read — the tool, its arguments, its outcome and its duration |
| `proposal` | a write was proposed instead of executed — with the arguments that will run if it is approved |
| `execution` | an approval was carried out, and whether the backend accepted it — **in the decision's own trace**, because the decision is a separate request |
| `memory` | a fact was recalled or remembered — the keys, never the values |

Two limits are stated rather than implied, because both are places a reader would otherwise assume more
than the code does: there is **one span per invocation rather than one per provider round trip** (the
`model_turns` count carries that number instead), and **retries are not spanned at all** — they happen
inside the MCP server, which returns a single outcome, so one `tool_call` span's duration covers every
attempt it made.

Attributes are **flat scalars only**, so the file is readable with `jq`; a structure worth keeping gets
its own span. Spans are written once at the end, atomically (`.partial` then `os.replace`), so a reader
never sees half a trace that looks complete. And tracing **never fails an invocation** — a write failure
is logged and reported through a callback rather than raised, because refusing to complete a run because
its explanation could not be recorded inverts the purpose.

### How I would use it

The concrete walkthrough, because "I'd look at the trace" is easy to claim:

> *"I asked the agent to raise a ticket for the jammed printer. It said it had, but there is no ticket."*

1. **Find the run** by session id in `traces`: `status = ok`, `tool_calls = 0`. Already informative — the
   turn succeeded and called nothing, so the model did not execute a write.
2. **Read the spans in order.** A `proposal` span for `create_ticket` is there, and no `execution` span
   follows it. The agent did not claim a write; it proposed one and stopped, and nobody approved it. The
   report and the trace disagree about what was *said*, not about what happened.
3. **If it had been approved and the ticket were still missing,** this trace is the wrong one — the
   decision is a separate request with a trace of its own. Take the second row for that session: it has
   `tool_calls = 1` and an `execution` span. Then the fork is one field: `outcome: executed` means the
   write reached the backend (the missing ticket is the mock's problem), while `outcome: failed` carries
   the reason, and whether retrying could ever have helped (`Authentication` never can).
4. **If the turn had given up,** the `tool_call` span shows the call that was made, its arguments and its
   `failed` outcome, and the tool's own sentence is where "attempted 4 times and has been given up on" is
   written. That is how "why did that take forty seconds and then fail?" is answered without guessing —
   it is the path in [`docs/media/05-failure-handled.png`](docs/media/05-failure-handled.png).
5. **For the case the design exists to survive** — a write whose response was lost — the `execution` span,
   the approval row's `executed_at` and the mock's own record are three independent sources, tied together
   by the `client_request_id` in the ledger. The trace shows an `Unresolved` error and **no second
   attempt**, and the ticket exists.

What a trace will not tell you: nothing about the model's *reasoning* (there is no chain-of-thought),
nothing from a previous process (the cancel registry is deliberately process-local), and nothing about
another user's run, because every read is scoped by `user_id` — including this one.

### Reading a trace without a database

```bash
uv run python -m devtasks traces                # every run on disk, newest first, failures marked
uv run python -m devtasks traces <id-prefix>    # one run, step by step, with durations
```

It reads `data/dbview/traces/` by default and needs neither the stack nor SQLite running. To put traces
there in the first place, start the stack with the database on the host:

```bash
docker compose -f docker-compose.yml -f docker-compose.dbview.yml up -d --build --wait
```

That replaces the agent's `/data` volume with `./data/dbview/`, so `agent.sqlite3` and `traces/*.jsonl`
become ordinary files a SQLite GUI can open while the stack runs. Watching `approvals.state` go from
`pending` to `executed` as you press Approve is the assignment's central guarantee, visible as a row.

### Exporting to a collector

The span format is mine — a small span model written to JSONL — because the requirement was a trace that
works in a container with no extra service. It is OTEL-shaped, and the service also **exports each
finished run over OTLP/HTTP** when an endpoint is configured:

```bash
docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one:latest
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 uv run python -m devtasks serve
```

The mapping is written over `httpx` rather than the OpenTelemetry SDK, and
[`observability/otlp.py`](services/agent/src/agent_service/observability/otlp.py) states plainly what that
gives up — no batching, retries, sampling or gRPC, and HTTP/JSON only. Model turns and tool calls carry
the GenAI attribute names (`gen_ai.operation.name`, `gen_ai.system`, `gen_ai.request.model`,
`gen_ai.tool.name`), so a collector renders a run as model and tool calls rather than as opaque spans. An
export can never delay or fail a turn: one POST with a five-second ceiling whose failure is logged.

---

## g. Key technical decisions

| # | Decision | Why |
|---|---|---|
| 1 | **Three services, not one** — mock, MCP server, agent | the agent cannot leak a credential it never holds, and the MCP server is where backend-shaped concerns (retries, the error taxonomy, the write ledger) live |
| 2 | **A plain loop over a framework**, six providers behind one interface | everything scored is state handling and control flow; a framework owning the state machine owns where the bugs are |
| 3 | **A uv workspace, one `uv.lock`, and a Python task runner** instead of Make plus per-service locks | one lock makes `--frozen` mean something across services; `python -m devtasks` works on Windows where Make often does not |
| 4 | **SQLAlchemy + Alembic over SQLite, with the runtime's session id as the primary key** | the approval must survive a restart, and a generated id would break the contract's promise that the same header resumes the same conversation |
| 5 | **A behavioural eval suite over the HTTP contract, runnable offline** | a suite that needs a key and a network is a suite that runs occasionally; assert tool choices deterministically instead |
| 6 | **Write detection derived from tool schemas** | a new write tool cannot be added and silently left ungated |
| 7 | **The approval is a row, and execution reads its arguments** | approving one thing and executing another is impossible by construction, not by discipline |
| 8 | **There is exactly one front end, and it is not in the agent** | two front ends meant two transcripts to keep correct; the page is gone and `GET /` is a 404 asserted by a test so it does not come back by accident |
| 9 | **CI's compose file is an override, not a second stack**, and is reachable from a terminal (`make up-ci`) | a topology only CI can start is a topology nobody can debug |
| 10 | **A separate uv project for the chat** | Chainlit requires `mcp<2`, the MCP server requires `mcp>=2`; one shared resolution would break the agent |

---

## h. Engineering standards — followed, and skipped

**Followed, and enforced by a command:**

- **mypy in strict mode** across the services, the tests and the dev tooling — no `any` in signatures, and
  no `# type: ignore` that is not explained.
- **ruff** for lint and formatting, with the ruleset pinned in `pyproject.toml`.
- **584 tests** that need no stack, no key and no network, plus integration tests against the composed
  stack for the rest.
- **Small modules with docstrings that explain *why*.** The docstrings are the design document at the code
  level: why spans are not rows, why a cancel does not interrupt a write, why an open span reports no
  duration rather than zero. Anything non-obvious can be read as "the alternative was X, and X is worse
  because Y".
- **A composition root** ([`factory.py`](services/agent/src/agent_service/factory.py)) so nothing
  constructs its own database session or transport.
- **Configuration from the environment only**, with credentials as `SecretStr`, and no configuration file
  to drift from the code.
- **Nothing claimed that a command has not shown.** Where a number appears in this documentation it was
  copied from output, and where something is unverified it says so — ARM64 being the standing example:
  the images cross-build, the *run* is not claimed.

**Skipped, deliberately or otherwise:**

- **No coverage threshold.** `make test` reports coverage; nothing fails on it. A number that discourages
  deleting a bad test is not obviously worth the enforcement.
- **No property-based testing.** The invariants here are few and specific — one ticket per approval, no
  cross-user read — and each has an example-based test.
- **No load, soak or stress testing.** Concurrency is the largest untested area in the design.
- **No structured logging library.** Standard `logging` with a per-request correlation id in the trace
  row; JSON logs were not needed locally and would have been untested output.
- **No authentication on the agent itself.** Every request carries a `user_id` and isolation is enforced,
  but the *provenance* of the id is trusted — the single honesty item in section c.
- **No accessibility audit or responsive test of the chat.** It is a thin client of the contract and the
  brief does not score visual design.
- **No `git` history rewriting, no signed commits, no changelog automation.** One commit per unit of work
  with a message that states what changed and what it cost.

---

## i. How I used AI tools in this development process

This repository was built with an AI coding assistant doing essentially all of the typing — code, tests
and documentation. That is the honest starting point, and the interesting part is therefore not *that* it
was used but **what the process around it was**, because the failure modes of AI-assisted engineering are
specific and predictable:

| Failure mode | The control that catches it here |
|---|---|
| Code that is plausible, reads well, and is wrong | the gates are commands, not opinions: `ruff`, `mypy --strict`, 584 tests, and integration tests against the composed stack, all green before any commit |
| Confident documentation that describes what was *intended* rather than what exists | every number in the docs is copied from real output; docs are written after the thing and against the running system |
| A feature that works and is unreachable from the running system | each piece of work was exercised through the real surface — over HTTP, in a container, with a browser |
| Scope drifting toward what is easy to generate | a plan written first, one piece of work at a time, each with what it would produce and how it would be checked |
| Tests that pass because they assert nothing | a deliberately broken fixture is scored by a unit test, and the secret scan was made to fail on purpose before it was trusted |
| Quietly editing the target to make the task easier | the mock is unmodified and checksum-verified in a test; the failure modes are the exercise |
| Losing the reasoning behind a decision | the decisions and their alternatives are recorded in this README rather than reconstructed later |
| A large diff that nobody reads | one focused change per commit, with a message that says what changed and what it cost |

**Do's, in the form I would hand to someone else:**

- Make the model work against a *verifiable* target: a gate, a test, a running service, a real command
  whose output you read. "Looks right" is the failure mode, not the tool.
- Write the plan and the definition of done first. Generated code is cheap; generated *scope* is what
  makes a project unmaintainable.
- Demand the reasoning, not just the result: why this shape and not the obvious alternative. It is also
  the fastest way to spot an invention.
- Prefer one small change per step, verified, over a large change that has to be trusted as a whole.

**Don'ts:**

- Don't accept a claim about behaviour that no command has printed. If it cannot be shown, it goes in the
  documentation as unverified or it does not go in at all.
- Don't let a check be added without proving it can fail. A check that cannot fail is worse than no check,
  because it gets cited as evidence.
- Don't let the tool edit the thing being tested, and prove it with a checksum rather than a promise.
- Don't ship generated prose as the answer to a question that asks for judgement — see section k.

---

## j. What I would do differently with more time

In rough priority order, and each is a real gap rather than a wish:

1. **Automate the chat UI in CI.** Screenshots prove it renders; nothing asserts the approval button does
   what it says. A handful of Playwright tests over the same interactions as the capture script would
   close the largest testing gap in the front end.
2. **Test concurrency.** The design assumes a conversation has one turn in flight and that the approvals
   table can be read and written from more than one process. Neither is asserted. Given the AgentCore
   target, this is the first thing I would build a test for — two instances against one database, one
   approval, exactly one execution.
3. **Add a nightly live-model run behind a secret.** The suite is deeper than most, but every gating case
   runs on a deterministic provider. One scheduled run against a real model, asserting only that a turn
   completes and picks a sensible tool, would catch prompt and tool-description regressions the scripted
   provider cannot.
4. **Make every environment variable either read or absent.** Several variables in this project were
   documented and read by nothing, and each presented as something else — a permissions error, a missing
   table, a feature described in a document that no code implements. The ones I found are now removed
   rather than labelled, and the ones that remain are wired. A startup assertion that the settings object
   and the template agree would remove the whole class.
5. **A `forget me` path for memory.** Facts expire; nothing deletes. That is the wrong answer for a user
   who asks, and it is a small feature with a clear contract.
6. **Coverage floor and mutation testing on the two critical modules** — the approval gate and the write
   ledger. They are the parts where an untested branch is a correctness bug rather than a gap.
7. **A second machine and a second Docker version** in the verification, because "works here" is not the
   same claim as "works", and this repository does not currently distinguish them.

---

## k. On whose thinking this is

The brief asks for my thoughts rather than an LLM's output, and it deserves a direct answer rather than a
footnote.

The code, the tests and the reference documentation here were produced with an AI assistant in the loop,
and section i describes the process in detail, including the controls that exist because of that. What
that means for this README is: the *descriptions* — how the approval state machine works, which error
sentence a model sees, what the last eval run printed — are mechanically checkable, and every one of them
points at a file, a command or a transcript you can open and disagree with. Nothing in them depends on
being trusted.

The judgement calls are the part to weigh, and they are recorded as decisions with their alternatives
rather than asserted as facts: why the loop is mine rather than a framework's, why two providers answer
without a model, why the front end is a client rather than a page, why the mock is never "fixed". Where I
have said something is unverified — an ARM64 run, the provenance of a `user_id`, a cost model — it is
because the honest answer was more useful than a confident one, and that preference is the same reasoning
the rest of this repository runs on: a claim nobody can check is worth less than a stated limitation.

If I were reviewing this submission, I would read the eval cases and the approval state machine first,
and this README second — the code is the part that can be regenerated.

---

## Commands

| Command | What it does |
|---|---|
| `make up` / `make down` | start / stop the whole stack |
| `make ps` | service status including health checks |
| `make logs` | tail logs (`make logs ARGS=agent` to narrow) |
| `make reset` | restore the mock's seed data |
| `make doctor` | what is installed, configured and actually answering |
| `make test` | whole suite with coverage |
| `make test-unit` | tests needing no stack and no model credential |
| `make test-integration` | tests needing the stack up |
| `make lint` / `make fmt` | ruff check / format |
| `make typecheck` | mypy, strict |
| `make migrate` | apply database migrations |
| `make eval` | run the eval suite and print a pass/fail table |
| `uv run python -m devtasks chat` | run the Chainlit front end on :8001 — needs the agent up |
| `uv run python -m devtasks traces <id>` | read one run's spans with no database |
| `make screenshots` | capture the run into `docs/media/` (`ARGS=--failure` for the fault case) |
| `make up-ci` / `make down-ci` | the CI stack, on its own compose project |
| `make ci` | lint + typecheck + unit tests + secret scan + evals |
| `make clean` | remove caches and local data |

## Notes for whoever runs this

- **The default model provider needs no key and no network**, so the stack runs, the tests pass and the
  evals pass on a machine with neither. The reference live provider is OpenAI — see section a.
- **Development happened on Windows and CI runs on Linux.** Everything is verified on both; a path that
  assumes one of them is a bug. That is not a preference — it is where two of the defects in this project
  came from.
- **Secrets never enter the repository or a log.** `.env` is gitignored, `detect-secrets` runs in the same
  form locally and in CI, and the scan has been made to fail on purpose before being trusted.
- **The mock is not ours to fix.** If a test needs different mock behaviour, the test is wrong — or it
  needs a different failure mode, which the mock already offers.

## License

MIT — see [`LICENSE`](LICENSE).
