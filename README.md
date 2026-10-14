# AgentCore Platform — IT Helpdesk Agent

An agent that answers IT-helpdesk questions and, when a change is actually required, proposes it and
waits for a human to approve before anything is written. Built for the AgentCore Platform Engineer
assignment (option 1).

> **Status: complete.** All sixteen stages are implemented and verified, and the whole thing was then
> re-verified from a **fresh clone** of the committed tree: 446 unit tests pass, all 22 eval cases
> pass, one command brings three services to healthy, and the recorded run regenerates — the commands
> and their output are in [`eval/results/latest.md`](eval/results/latest.md). Stage 17 then closed the
> four gaps that re-reading the assignment against the *running* system turned up — including the
> contract's own smoke test, which now builds and runs.
> The reasoning for each fix is in the commit message that made it.
>
> Two tags, deliberately rather than one moved: **`v1.0.0`** marks the tree stage 16 verified from a
> fresh clone, and **`v1.0.1`** marks this one, which adds the contract-compliance fixes. Moving the
> first would have been a rewrite of a tag that had already been pushed; a second tag says which tree
> is which without pretending nothing changed. Every claim in this README is either a command whose
> output is quoted verbatim or a pointer to the file that contains it; anything not verified says so.

**The short version.** Three processes: a mock ticketing API that fails on demand, an MCP server that
is the only component holding a ticketing credential, and an agent that runs a model loop against it.
The agent proposes writes and stops; a person approves; the approval is a database row, so it survives
a restart. Failures are described to the model in sentences it can act on. Every run leaves a trace
that explains it. `make up && make eval` runs the whole thing — 22 cases, no API key, no network.

---

## a. Quick setup

Requires Docker Desktop. For the development loop (lint, types, tests, evals) you also want Python
3.12 and [uv](https://docs.astral.sh/uv/). **Running the stack needs neither Python nor uv on the
host.**

```bash
git clone https://github.com/serhiiliutov/agentcore-platform && cd agentcore-platform
cp .env.example .env          # every value has a working default
docker compose up -d --wait   # starts all three services, blocks until they are healthy
```

`--wait` is the whole definition of done in one command: it exits non-zero if any healthcheck never
passes. Then:

| | |
|---|---|
| **The page** | http://localhost:8080/ — type a problem, watch the tool calls, approve or decline |
| Agent contract | `POST http://localhost:8080/invocations` |
| Agent health | `GET http://localhost:8080/ping` |
| Ticketing mock | http://localhost:8000/docs |
| Mock fixture reset | `make reset` |

A first conversation, from a terminal:

```bash
curl -s localhost:8080/ping
# {"status":"Healthy"}

curl -s -X POST localhost:8080/invocations -H 'Content-Type: application/json' \
  -d '{"prompt":"find tickets about the printer","user_id":"alice","session_id":"demo-1"}'
# stop_reason: completed, tool_calls: [search_tickets -> executed]
```

`user_id` is optional and defaults to one shared scope, `anonymous` — which is why the contract's own
smoke-test body (`{"prompt": "..."}`, no user id at all) answers instead of failing validation, and
why two callers who both omit it share one memory. Each tool call carries the contract's field names
and this service's side by side — `arguments` with `input`, `outcome` with `status` — plus
`duration_ms` and a one-sentence `summary`;
[`api/models.py`](services/agent/src/agent_service/api/models.py) is where that pairing is decided
and explained.

**The contract's single-container smoke test also runs**, from this repository root:

```bash
docker build -t my-agent . && docker run -p 8080:8080 my-agent
```

One container holding the agent, the MCP server (over stdio) and the ticketing mock. Its
[`standalone-entrypoint.sh`](standalone-entrypoint.sh) repairs — and logs — the three values in
`.env.example` that only make sense under Compose: a service hostname in `TICKETING_BASE_URL`, the
HTTP MCP transport, and a `TICKETING_API_KEY=${API_KEY}` that `docker run --env-file` passes through
literally rather than interpolating. The three-service topology above is unchanged and is still what
`docker compose up` gives you; this image exists because `CONTRACT.md` opens with exactly that
command.

For the development loop:

```bash
uv sync --all-groups     # one environment for the whole workspace
make help                # every command, with an example
make doctor              # what is installed, what is configured, what is answering
make test-unit           # 446 tests, no stack and no credentials needed
make eval                # starts its own stack and runs the 22 cases (exit 1 on any failure)
```

`make` is a thin wrapper: every target is one line delegating to `uv run python -m devtasks <command>`,
which also works on Windows where GNU Make usually is not installed. Both forms run the same code.

**To see it fail on purpose** (the assignment asks for a mock that fails some of the time):

```bash
FLAKY=1 FLAKY_RATE=0.3 make mock              # ~30% of calls fail, reproducibly
LATENCY_MS=3000 docker compose up ticketing   # every response takes 3 seconds
```

## Start here — four questions, answered with commands

Written for someone who has just cloned this. PowerShell on Windows; on Linux or macOS write
`VAR=value` instead of `$env:VAR='value'`.

**Run it.** No key, no network and no configuration: the default provider is the deterministic
`scripted` one, so the stack comes up working.

```powershell
docker compose up -d --wait        # ticketing mock + MCP server + agent, ~20s (same as: make up)
start http://localhost:8080/       # the page: ask something, watch it propose, approve it
docker compose logs -f agent       # what the agent is doing, as it does it
docker compose down                # stop; add -v to drop the data volume too
```

The page is served by the agent container itself — there is no separate web service, no nginx and no
front-end build. `make doctor` prints what is installed, what is configured and what is answering.

**Test it.** Three levels, none of which needs a key:

```powershell
uv run python -m devtasks test-unit         # 446 tests in ~7s: no stack, no credentials
uv run python -m devtasks eval              # 22 cases, its own stack, exit 1 on any failure
uv run python -m devtasks test-integration  # the 4 tests that need the stack from the line above
```

**`make` is optional and on Windows it is usually absent** — those three lines are exactly what
`make test-unit`, `make eval` and `make test-integration` run (`make logs` is
`docker compose logs -f agent`). Needs `uv sync --all-groups` once.

Then drive the contract by hand, because that is the surface the assignment describes — two turns of
one conversation:

```powershell
$h = @{ 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id' = 'try-1' }

# 1. it proposes the write and stops instead of writing
Invoke-RestMethod http://localhost:8080/invocations -Method Post -Headers $h -ContentType application/json `
  -Body (@{ prompt = 'open a ticket for the flickering monitor'; user_id = 'me' } | ConvertTo-Json) |
  Select-Object stop_reason, pending_approval

# 2. answering in the same session IS the approval — the ticket is created now
Invoke-RestMethod http://localhost:8080/invocations -Method Post -Headers $h -ContentType application/json `
  -Body (@{ prompt = 'yes, go ahead'; user_id = 'me' } | ConvertTo-Json) |
  Select-Object stop_reason, tool_calls

# the other decision path, using the approval id from step 1: same gate, one row, one write
Invoke-RestMethod http://localhost:8080/invocations/approvals/<approval_id>/decide -Method Post `
  -ContentType application/json -Body (@{ user_id = 'me'; approved = $true } | ConvertTo-Json)
```

Say `"no thanks"` instead and nothing is written; ask something unrelated and the proposal stays
pending and comes back on the response. `eval/cases.json` asserts exactly those three answers.

**Switch models.** One environment variable, read once when the process starts:

```powershell
$env:ANTHROPIC_API_KEY = 'sk-ant-...'      # or OPENAI_API_KEY; OPENAI_BASE_URL for a local model
$env:AGENT_MODEL_PROVIDER = 'anthropic'    # scripted | replay | anthropic | openai |
                                           # openai_compatible | bedrock
docker compose up -d agent                 # recreate the agent with the new environment
```

`AGENT_MODEL_ID` names a specific model and is empty by default, meaning the provider's own default.
There is no runtime switch and no provider picker: the choice is read once while the application is
built, so changing it is an environment change and a restart, and a provider selected without what it
needs is refused at startup with a message naming it. Full table, per-provider examples, and the
local-model caveat: section c and `.env.example`.

**Read it.** In this order. Each file is self-contained; the first two tell you where everything else
lives.

| # | File | Why start there |
|---|---|---|
| 1 | `services/agent/src/agent_service/api/app.py` | the whole contract surface — `/ping`, `/invocations`, the session header, and every route it mounts |
| 2 | `.../agent_service/factory.py` | the composition root: which implementation of each interface this process got, and why |
| 3 | `.../agent_service/agent/loop.py` | the loop — prompt → provider → the tools that may run → repeat, with the one interception that turns a write into a proposal |
| 4 | `.../agent_service/agent/decisions.py` | what counts as yes, what counts as no, and what counts as neither |
| 5 | `.../agent_service/agent/handler.py` | one turn end to end: memory, the decision gate, the span it writes |
| 6 | `.../agent_service/agent/gate.py`, `.../execution.py` | the approval state machine, and the write that happens exactly once |
| 7 | `.../agent_service/tools/registry.py` | which tools are reads and which are writes — the rule the interception uses |
| 8 | `.../agent_service/db/models/` | the schema, and the two things deliberately not in it |
| 9 | `tests/unit/test_message_approval.py`, `eval/cases.json` | the behaviour, as executable assertions rather than prose |
| 10 | [`AGENTCORE.md`](AGENTCORE.md) | how this maps onto AWS Bedrock AgentCore, what I would keep and change, and what I would verify before relying on it |

## b. Architecture overview

Three processes, and the agent never touches the ticketing system directly:

```
                 ┌────────────────────────────┐
   browser ─────▶│  agent                :8080│  AgentCore Runtime workload
                 │  GET  /                    │  the page: the transcript, with
                 │  POST /invocations         │  approve · decline · cancel
                 │  GET  /ping                │  model loop · approval · memory
                 └────────────┬───────────────┘  traces
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

* **A write cannot happen without a human.** Write tools are intercepted before execution and become
  a pending approval. Only an explicit approval executes the call, and the pending state is a
database row, so it survives a restart. → `db/models/approval.py`, `agent/gate.py`
* **A failure is described, not swallowed.** Rate limits, 500s and lost responses are handled inside
  the MCP server and reported to the model as structured, actionable errors. →
  the taxonomy in `services/mcp-ticketing/src/ticketing_mcp/errors.py` and
  `services/agent/src/agent_service/agent/budget.py`
* **Any run can be explained afterwards.** Every turn, every tool call with its arguments and
  outcome, every proposal and every approval decision is a span or an event in a trace you can read
  — and the two things deliberately *not* traced (one span per invocation rather than per provider
  round trip; retries, which happen inside the MCP server) are named in that document rather than
  left to be discovered. → `observability/spans.py`, `observability/tracer.py`

The full tour — the request path, the module map, where each concern lives — is
`services/agent/src/agent_service/factory.py`. The five decisions that shaped it are recorded in the
five decision records under [`docs/adr/`](docs/adr/).

> **`docs/` is internal and is not published.** The only parts of it in this repository are the
> decision records (`docs/adr/`), the screenshots and the eval console run (`docs/media/`), and the
> starter kit's own documentation and checksum manifest (`docs/starter-kit/`, which a unit test reads
> to prove the mock fixture has not been edited). Everything else that used to live under `docs/` —
> the plan of record, the architecture notes, the runbooks and the assignment brief — is working
> material, not a deliverable, and has been removed from the repository. It is kept locally; nothing
> in this README, in `AGENTCORE.md`, or in the code depends on it.

---

## c. Agent design

### What I considered, and what I chose

The brief allows any framework — LangGraph, Strands Agents, the OpenAI Agents SDK — or plain code,
and any model provider. I chose **a plain, explicit loop in our own code**, and **six providers behind
one narrow interface**. The reasoning is in [`ADR-0002`](docs/adr/0002-agent-orchestration-and-model-providers.md);
the short version is that everything this assignment scores is a property of *state handling and
control flow*, and a framework that owns the state machine also owns the place the bugs will be.

**LangGraph** was the strongest candidate and the closest call. Its checkpointer and
interrupt/resume primitives map onto the approval requirement, but the gate needs to be
*structural* — a write tool that physically cannot execute during the model loop — and a framework's
interrupt is a cooperative convention. I would still have had to build the interception, the
idempotency and the durability on top of it, and would then have been debugging a checkpoint
encoding to answer questions the assignment asks directly. **Strands Agents** is thinner and
AWS-aligned, and would have let `AGENTCORE.md` speak to a first-party framework; its value here (model
abstraction) is the part I can write in ~100 lines with no dependency risk. The **OpenAI Agents SDK**
pulls the design toward provider-specific primitives, which fights the swappability requirement.

**The loop is under 300 lines** (`agent/loop.py`), which is the point: assemble the prompt → ask the
provider → execute the tool calls that are allowed to run → repeat until the model answers or a
budget is exhausted, with one interception layer that turns a write call into a proposal. Every piece
of state it touches is a table I can query, so "why did this run do that?" is answered with SQL rather
than by reading a framework's internals.

**Providers** are selected by configuration, per process — one provider answers for the whole service,
and a different one means changing the environment and restarting:

| Provider | Purpose | Needs a key |
|---|---|---|
| `scripted` | deterministic, rule-driven responses — the default | no |
| `replay` | replays a recorded live run | no |
| `anthropic` | Claude, native tool use | yes |
| `openai` | GPT, chat completions with tools | yes |
| `openai_compatible` | **any** OpenAI-shaped endpoint: Ollama, vLLM, LM Studio, Groq, Together | usually no |
| `bedrock` | Amazon Bedrock Converse API | AWS credentials |

`scripted` is not a workaround for not having a key — it is what makes the eval suite reproducible
and free enough to run on every commit, and it is why every screenshot in this repository can be
regenerated. Keys are `SecretStr` so a `repr()` in a log or a trace shows `**********`, and no key, key
prefix or fingerprint is published by any endpoint. There is no provider picker and no runtime switch:
choosing a provider is the environment change and restart the runbook describes, and one selected
without what it needs is refused while the application is built — with a message naming it — rather
than failing three turns into a conversation.

The honest limitation, measured rather than assumed: local inference here means a ~3B model on CPU
(4 GB GPU with no Windows container pass-through, 16 GB RAM), which is slow and unreliable at tool
selection. It is offered as proof the provider is genuinely swappable, and it is never what the demo
or the evals run on.

### How state and the approval step work

**State lives in the database and nowhere else.** Sessions and messages, approvals, facts, traces and
the idempotency ledger are tables; the only in-memory state is the registry of *currently running*
turns, which is deliberately process-local because a cancel has to reach a coroutine in this process.
A restart therefore loses nothing that matters — and there is an eval case that restarts the service
between proposing a write and approving it.

**The approval step is a state machine with a row behind it** (full detail in
`db/repositories/approvals.py`):

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
(`{"prompt": "yes, go ahead"}`); the page needs a button that acts on a specific approval id. Both
work: `agent/decisions.py` classifies a message, `POST /invocations/approvals/{id}/decide` takes the
click, and both call the same `ApprovalGate.decide` — one implementation, one row, one write. A
message the classifier is unsure about ("yes, but first find me the printer ticket") is *not*
consent: the proposal stays pending and the turn is answered normally, which is what makes a
digression safe rather than destructive.

Four properties make it real rather than decorative:

1. **The proposal stores the exact arguments.** Execution never goes back to the model for
   parameters, so approving one thing and executing another is impossible by construction.
2. **The refusal is enforced in the repository, not the route.** A second decision on a decided row is
   a 409 from the layer that owns the state, so a new call site cannot accidentally allow it.
3. **`declined` and `failed` are different states**, because "I said no" and "it did not work" are
   different things to tell a user.
4. **The idempotency key is minted before approval**, not inside the tool, and is unique on the table
   — which is what makes a replayed execution create one ticket rather than two.

Which tools are writes is **derived from their schemas**, not from a hand-maintained list: the MCP
server requires `client_request_id` on writes, and the agent's gate branches on that same fact. Add a
fifth write tool to the server and it is gated automatically.

### What memory keeps, where, and for how long

Two kinds of memory, deliberately separate:

| | Short-term | Long-term |
|---|---|---|
| **What** | the conversation so far | one durable fact: `requester_name`, `team`, `preferred_category` |
| **Where** | `sessions` + `messages` tables | `user_facts` table, same database |
| **How long** | the session; never swept | `expires_at` = `AGENT_MEMORY_FACT_TTL_DAYS` (default 180), evaluated on read |
| **For whom** | one user, one session | one user, across all their sessions; `user_id` on every read |

Three choices are worth naming because the alternatives are more common:

- **Extraction is deterministic regexes over the user's own prompt**, not a model call. The keys are a
  fixed whitelist, so the only thing a model extractor could add is a second way to be wrong — and a
  regex is reviewable, testable without a provider, and identical on every run.
- **Only the user's prompt is read** — not the assistant's reply, not a tool result, not a ticket
  body. The danger in remembering is not a missed fact; it is a ticket description becoming durable
  state about a person. One field with three known keys is a rule that cannot leak.
- **Facts are injected into the system prompt per turn and never written back to the transcript**, so
  the conversation the next turn reads is the one that actually happened, and an expired fact stops
  appearing without anything being deleted.

A reviewer can list everything remembered about a person without reading any application code:

```sql
SELECT key, value, source_session_id, expires_at FROM user_facts WHERE user_id = ?;
```

`source_session_id` records which conversation taught us the fact, so "why do you think that about
me?" is answerable. The limitation I would want stated plainly: **isolation** is enforced and tested
(one user's facts are invisible to another), but **provenance** is not — `user_id` arrives in the
request body, so a client that lies about it sees someone else's memory. On AgentCore, Identity would
make that an authentication property instead of a convention. That gap is in the verification table
of [`AGENTCORE.md`](AGENTCORE.md). Design detail: `agent/memory.py`.

## d. Tool design

Four tools, two reads and two writes, exposed over MCP by `mcp-ticketing`. The full catalogue with
schemas and error contracts is `services/mcp-ticketing/src/ticketing_mcp/tools.py`.

| Tool | Kind | Notes |
|---|---|---|
| `search_tickets` | read | filters + pagination; returns `total` and `has_more` |
| `get_ticket` | read | one ticket, with comments |
| `create_ticket` | **write** | requires `client_request_id` |
| `add_comment` | **write** | requires `client_request_id` |

**Naming.** `snake_case`, verb-first, describing the action on the domain object rather than the HTTP
method underneath — `search_tickets`, not `get_tickets` or `list`. `search_tickets` is a *search*
rather than a listing because it takes free text and returns a page, and the word tells the model the
result may be partial.

**Describing.** Each description is one paragraph written for a model, answering the same four
questions in the same order: what it does, what it returns, when to use it, what it does not do. They
are passed through to the provider verbatim rather than restated in the agent, because two copies
would be two versions that could disagree. The concrete example: `search_tickets` returns `total` and
`has_more` *because* a model shown ten rows without them cannot tell "these are all the matches" from
"these are the first ten of forty", and will state the wrong thing confidently.

**How errors reach the model.** They are **returned, not raised** — a raised exception would cross the
protocol boundary as a transport fault and lose the only thing the agent needs to recover: which kind
of failure this was. Six kinds, each with a sentence written as advice rather than a status code:

| Kind | The sentence |
|---|---|
| `NotFound` | "No ticket with that id exists. Check the id before retrying." |
| `Validation` | "The request was rejected: … Fix the arguments." |
| `Authentication` | "The ticketing API rejected the credential. This is a configuration problem, not a retry." |
| `RateLimited` | "The ticketing API is rate limiting this client. Waiting is required before retrying." |
| `IdempotencyConflict` | "This client_request_id was already used for a different request." |
| `Unresolved` | "The write's outcome could not be confirmed and it was NOT retried, because it may have succeeded. Check the ticket list before deciding whether to try again." |

The last one is the important one. When a write's response is lost the outcome is genuinely unknown,
so the tool does not retry it, does not claim success and does not claim failure — it tells the model
to go and look. `failures_do_not_duplicate_a_write` asserts the ticket count afterwards.

One more detail that only shows up under load: the exhausted-retry sentence **replaces** the per-kind
one. "Waiting is required before retrying" is correct for a single 429 and actively wrong after four
attempts have already failed, because it recommends the one thing that has just been established not
to work. The screenshot [`05-failure-handled.png`](docs/media/05-failure-handled.png) is that path.

**How the MCP server authenticates to the mock.** The mock requires `X-API-Key` on `/tickets` and
`/admin`, checked in one middleware before anything else. **Only `mcp-ticketing` holds that key** —
the agent has no ticketing credential at all, which is not a stylistic boundary but the reason the
agent cannot leak a credential it never has. A credential never appears in a tool result, a log or a
trace; a test plants a canary key in the environment and asserts it never reaches a provider request
or a response.

---

## e. Quality: the evals, the results, and what they miss

One command, `make eval`, runs **22 cases against the real service over HTTP** and exits non-zero if
any fails. No API key and no network: the suite drives the deterministic `scripted` provider, so the
same commit produces the same table. The committed transcripts are
[`eval/results/latest.md`](eval/results/latest.md) (the report) and
[`docs/media/eval-run.txt`](docs/media/eval-run.txt) (the console run).

### The latest result — 22 of 22 passed in 46.8s

| Family | Cases | What it proves |
|---|---|---|
| **Right tool chosen** | `right_tool_search`, `right_tool_read`, `noise_then_search`, `pagination_page_two` | the model picks the right tool for the request, including when noise precedes it, and reads page two when the first page is not enough |
| **Approval step respected** | `approval_respected`, `approval_declined_writes_nothing`, `invalid_enum_handled`, `digression_then_resume`, `approval_expiry`, `restart_between_propose_and_approve`, `approval_by_message`, `decline_by_message`, `unrelated_message_keeps_the_proposal` | a write is proposed and not executed; a declined or expired one reaches the mock **zero** times; a pending approval survives a restart; a digression mid-approval does not lose it; and the contract's own flow — answering "yes, go ahead" in the next call — executes exactly one write, while "no thanks" writes nothing and an unrelated message leaves the proposal pending |
| **Out-of-scope request refused** | `out_of_scope_refused`, `first_contact` | the agent declines work outside the helpdesk and says why |
| **A run with failures switched on** | `failures_switch_on`, `not_found_clarification`, `failures_do_not_duplicate_a_write` | retries happen, give up is reported honestly, and a lost write is not repeated |
| **A run that runs out of time** | `timeout_gives_up` | a slow backend produces a truthful timeout rather than a hang |
| **Memory** | `memory_across_sessions`, `memory_isolation_between_users` | a fact told in one session is used in another, and one user never sees another's |
| **The key never appears** | `secret_never_leaked` | no credential reaches a response |

Two things about that table are worth stating rather than leaving for the reader to assume. It is
**not** a claim that the suite cannot pass vacuously: `eval/fixtures/deliberately-broken.json` is
scored by a unit test, and running it against the live stack makes `make eval` exit 1 — a suite that
cannot fail is worse than none, because it gets cited as evidence. And it is **not** a claim about a
live model: the cases assert deterministic tool choices, which is what makes them reproducible.

### What the evals do not catch

Named rather than implied, because a suite's gaps matter more than its pass count:

- **Live-model behaviour.** Every case runs on `scripted`. The live providers are covered by unit
  tests against recorded payloads, which verifies the message translation and tool-call parsing but
  nothing about judgement. A live run is a different exercise, and
  no live run is recorded here: the development host has no model credential.
- **Latency and cost.** Nothing asserts a ceiling on either. The turn deadline is tested by
  exhausting it deliberately, not by measuring a real slow run end to end.
- **Concurrency.** Two turns in one session at once, or two instances sharing the database, are
  untested — the design assumes a conversation has at most one turn in flight, and that assumption is
  not asserted under load.
- **The page.** Screenshots are produced by a real browser (`make screenshots`), but no test asserts
  the UI's behaviour; the evals drive the HTTP contract only.
- **Everything platform-shaped.** ARM64 is cross-built, not run; the containers are verified healthy
  but not load-tested; and nothing here has been run on a second machine with a different Docker
  version.
- **Memory extraction beyond three keys.** The regexes are tested, including their known
  misreadings, but "remember anything useful the user said" is explicitly not the promise.

### Guardrails

Beyond the features, the things that stop a class of mistake rather than fixing an instance of it:

| Guardrail | What it prevents |
|---|---|
| The approval gate itself | any write without a human, structurally rather than by prompt instruction |
| Write detection derived from tool schemas | a new write tool being added and silently not gated |
| `client_request_id` unique per approval | a double execution creating two tickets under any retry |
| The idempotency ledger keyed on request fingerprint | a retried `/invocations` running the model (and proposing) twice |
| The turn deadline and the step ceiling | a stuck loop; a loop that cannot terminate is a bug with a name |
| Cooperative cancellation | a cancel landing mid-write and leaving an unknown outcome |
| `SecretStr` + a canary test | a key reaching a log, a trace or a response |
| `user_id` on every read, no unscoped path | cross-user leakage, including in traces |
| The mock's checksums | accidentally "fixing" the thing whose failure is the exercise |
| `detect-secrets` in CI, proven able to fail | a credential being committed with a green pipeline |
| The per-stage plan and its definition of done | a claim in the docs that no command supports |

That last row is the one I would keep above all the others, and it is not a code guardrail. Every
piece of work started from a written definition of done, checked against the running system; several
of those checks found that a feature existed and was unreachable from the running system — the web
page, and the CI compose file. Both were "working" and neither could be reached.

## f. Observability: what is traced, and how I'd debug a bad run

### What is recorded

One invocation produces **one trace**, as two artefacts: a JSONL file of spans, and one indexed row in
the `traces` table (session, user, duration, status, provider, model, counters, error, and the path to
the file). The row answers *"find me the run"*; the file answers *"explain this run"*. Design and
trade-offs: `observability/tracer.py`.

Five span kinds, a closed vocabulary:

| Span | Recorded when |
|---|---|
| `model_turn` | once per invocation, around the whole loop — provider, `model_turns`, duration, and whatever tokens the provider reported |
| `tool_call` | each executed read — the tool, its arguments, its outcome and its duration |
| `proposal` | a write was proposed instead of executed — with the arguments that will run if it is approved |
| `execution` | an approval was carried out, and whether the backend accepted it — **in the decision's own trace**, because the decision is a separate request |
| `memory` | a fact was recalled or remembered — the keys, never the values |

Two limits are stated rather than implied, because both are places a reader would otherwise assume
more than the code does: there is **one span per invocation rather than one per provider round trip**
(the `model_turns` count carries that number instead), and **retries are not spanned at all** — they
happen inside the MCP server, which returns a single outcome, so one `tool_call` span's duration
covers every attempt it made.

Attributes are **flat scalars only**, so the file is readable with `jq`; a structure worth keeping gets
its own span. Spans are written once at the end, atomically (`.partial` then `os.replace`), so a reader
never sees half a trace that looks complete. And tracing **never fails an invocation** — a write
failure is logged and reported through a callback rather than raised, because refusing to complete a
run because its explanation could not be recorded inverts the purpose.

### How I would use it

The concrete walkthrough, because "I'd look at the trace" is easy to claim:

> *"I asked the agent to raise a ticket for the jammed printer. It said it had, but there is no
> ticket."*

1. **Find the run** by session id in `traces`: `status = ok`, `tool_calls = 0`. Already informative —
   the turn succeeded and called nothing, so the model did not execute a write.
2. **Read the spans in order.** A `proposal` span for `create_ticket` is there, and no `execution`
   span follows it. The agent did not claim a write; it proposed one and stopped, and nobody approved
   it. The report and the trace disagree about what was said, not about what happened.
3. **If it had been approved and the ticket were still missing,** this trace is the wrong one — the
   decision is a separate request with a trace of its own. Ask for the session's traces and take the
   second row: it has `tool_calls = 1` and an `execution` span. Then the fork is one field: an
   `execution` span with `outcome: executed` means the write reached the backend (the missing ticket
   is the mock's problem), while `outcome: failed` carries the reason, and whether retrying could
   ever have helped (`Authentication` never can).
4. **If the turn had given up,** the `tool_call` span shows the call that was made, its arguments and
   its `failed` outcome, and the tool's own sentence is where "attempted 4 times and has been given
   up on" is written — the retries are inside the MCP server rather than separate spans, so the one
   span's duration covers all of them. That is how "why did that take forty seconds and then fail?"
   is answered without guessing, and it is the path in
   [`05-failure-handled.png`](docs/media/05-failure-handled.png).
5. **For the case the design exists to survive** — a write whose response was lost — the `execution`
   span, the approval row's `executed_at` and the mock's own record are three independent sources,
   tied together by the `client_request_id` in the ledger. The trace shows an `Unresolved` error and
   **no second attempt**, and the ticket exists.

What a trace will not tell you: nothing about the model's *reasoning* (there is no chain-of-thought),
nothing from a previous process (the cancel registry is deliberately process-local), and nothing about
another user's run, because every read is scoped by `user_id` — including this one.

The format is mine — a span model written to JSONL — rather than OTEL, because the requirement was a
trace that works in a container with no extra service. It is deliberately shaped like OTEL (kind,
name, timestamps, duration, flat attributes, order implying parent) so that moving to
AgentCore Observability is occupying a real SDK and keeping the names; the service already carries an
`OTEL_EXPORTER_OTLP_ENDPOINT` setting for exactly that. Items 8 and 9 of [`AGENTCORE.md`](AGENTCORE.md)
are what I would verify before relying on it.

---

## g. Key technical decisions

Each of these has an ADR with the alternatives it rejected; the table is the summary.

| # | Decision | Why | ADR |
|---|---|---|---|
| 1 | **Three services, not one** — mock, MCP server, agent | the agent cannot leak a credential it never holds, and the MCP server is where backend-shaped concerns (retries, the error taxonomy, the write ledger) live | [0001](docs/adr/0001-monorepo-and-service-boundaries.md) |
| 2 | **A plain loop over a framework**, six providers behind one interface | everything scored is state handling and control flow; a framework owning the state machine owns where the bugs are | [0002](docs/adr/0002-agent-orchestration-and-model-providers.md) |
| 3 | **uv workspace, `uv.lock`, and a Python task runner** instead of Make + per-service locks | one lock makes `--frozen` mean something across services; `python -m devtasks` works on Windows where Make often does not | [0003](docs/adr/0003-dependency-management-and-task-running.md) |
| 4 | **SQLAlchemy + Alembic over SQLite, with the runtime's session id as the primary key** | the approval must survive a restart, and a generated id would break the contract's promise that the same header resumes the same conversation | [0004](docs/adr/0004-persistence-and-migration-strategy.md) |
| 5 | **A behavioural eval suite over the HTTP contract, runnable offline** | a suite that needs a key and a network is a suite that runs occasionally; assert tool choices deterministically instead | [0005](docs/adr/0005-behavioural-evaluation.md) |

Four further decisions that did not need an ADR but carry weight:

- **Write detection derived from tool schemas.** The MCP server requires `client_request_id` on
  writes; the agent's gate branches on that same fact, so a new write tool cannot be added and
  silently left ungated.
- **The approval is a row, and execution reads its arguments.** Approving one thing and executing
  another is impossible by construction, not by discipline.
- **The page is served by the agent** as a Python constant, with no static-file mount and no build
  step. The brief says the page is not scored on visual design; spending nothing on tooling means
  nothing between a reviewer and a working demo.
- **CI's compose file is an override, not a second stack** — and it is reachable from a terminal
  (`make up-ci`), because a topology only CI can start is a topology nobody can debug. The reasoning,
  including why Compose's `include:` was rejected, is in the header comments of
  `docker-compose.ci.yml` and `.github/workflows/ci.yml`.

## h. Engineering standards — followed, and skipped

**Followed, and enforced by a command:**

- **mypy in strict mode** across the services, the tests and the dev tooling — 104 files, no `any` in
  signatures, and no `# type: ignore` that is not explained.
- **ruff** for lint and formatting, with the ruleset pinned in `pyproject.toml`.
- **446 tests** that need no stack, no key and no network (unit and contract, plus the eval scorer's
  own tests), and integration tests against the composed stack for the rest.
- **Small modules with docstrings that explain *why*.** The docstrings are the design document at the
  code level: why spans are not rows, why a cancel does not interrupt a write, why an open span
  reports no duration rather than zero. Anything non-obvious in this repository can be read as
  "the alternative was X, and X is worse because Y".
- **A composition root** (`factory.py`) so nothing constructs its own database session or transport.
- **Configuration from the environment only**, with credentials as `SecretStr`, and no
  configuration file to drift from the code.
- **A written definition of done before each piece of work**, and a commit per piece that records what
  it cost — including the defects it found. Nine of those defects came from one piece of work alone.
  Those records are internal working material; what they produced is the code and the tests beside it.
- **Nothing claimed that a command has not shown.** Where a number appears in this documentation it
  was copied from output, and where something is unverified it says so — ARM64 being the standing
  example: the images cross-build, the *run* is not claimed.

**Skipped, deliberately or otherwise:**

- **No coverage threshold.** `make test` reports coverage; nothing fails on it. A number that
  discourages deleting a bad test is not obviously worth the enforcement.
- **No property-based testing.** The invariants here are few and specific (one ticket per approval,
  no cross-user read), and each has an example-based test. Hypothesis would have earned its place for
  the argument canonicalisation in the ledger, which is the one place hidden input variation could
  bite.
- **No load, soak or stress testing.** Concurrency is the largest untested area in the design.
- **No structured logging library.** Standard `logging` with a per-request correlation id in the
  trace row; JSON logs were not needed locally and would have been untested output.
- **No authentication on the agent itself.** Every request carries a `user_id` and the isolation is
  enforced, but the *provenance* of the id is trusted — the single honesty item in section c.
- **No accessibility audit or responsive test of the page.** It is a thin client of the contract and
  the brief does not score it; the UI's automated coverage is screenshots, not assertions.
- **No `git` history rewriting, no signed commits, no changelog automation.** One commit per unit of
  work with a message that states what changed and what it cost.

## i. How I used AI tools in this development process

This repository was built with an AI coding assistant doing essentially all of the typing — code,
tests and documentation. That is the honest starting point, and the interesting part of the answer is
therefore not *that* it was used but **what the process around it was**, because the failure modes of
AI-assisted engineering are specific and predictable:

| Failure mode | The control that catches it here |
|---|---|
| Code that is plausible, reads well, and is wrong | the gates are commands, not opinions: `ruff`, `mypy --strict`, 446 tests, and integration tests against the composed stack, all green before any commit |
| Confident documentation that describes what was intended rather than what exists | every number in the docs is copied from real output; docs are written *after* the thing and against the running system. This caught six architecture documents that were linked and never written |
| A feature that works and is unreachable from the running system | each stage's definition of done is exercised through the real surface — over HTTP, in a container, with a browser. This caught the web page (stage 11) and the CI compose file (stage 14) both existing and being unreachable |
| Scope drifting toward what is easy to generate | a plan of record written first, one piece of work at a time, each with what it would produce and how it would be checked |
| Tests that pass because they assert nothing | a deliberately broken fixture is scored by a unit test, and the secret scan was made to fail on purpose before it was trusted. A check that cannot fail is worse than no check, because it gets cited as evidence |
| Quietly editing the target to make the task easier | the mock is unmodified and checksum-verified in a test; the failure modes are the exercise |
| Losing the reasoning behind a decision | ADRs, each recording the option chosen, the options rejected and why — written when the decision is made, not reconstructed later |
| A large diff that nobody reads | one focused change per commit, with a message that says what changed and what it cost; the defect logs are the substance, not the feature list |

**Do's, in the form I would hand to someone else:**

- Make the model work against a *verifiable* target: a gate, a test, a running service, a real
  command whose output you read. "Looks right" is the failure mode, not the tool.
- Write the plan and the definition of done first. Generated code is cheap; generated *scope* is what
  makes a project unmaintainable.
- Demand the reasoning, not just the result: why this shape and not the obvious alternative. That is
  what the docstrings and ADRs are for, and it is also the fastest way to spot an invention.
- Keep the loop honest by reading failure output — including the failures that only appear in a
  terminal at 2am, like a `--wait` that returns before a container is healthy.
- Prefer one small change per step, verified, over a large change that has to be trusted as a whole.

**Don'ts:**

- Don't accept a claim about behaviour that no command has printed. If it cannot be shown, it goes in
  the documentation as unverified or it does not go in at all.
- Don't let a check be added without proving it can fail. Two of the nine defects in stage 14 were
  gates that would have passed with a live credential in the tree.
- Don't let the tool edit the thing being tested, and prove it with a checksum rather than a promise.
- Don't ship generated prose as the answer to a question that asks for judgement — see section k.

## j. What I would do differently with more time

In rough priority order, and each is a real gap rather than a wish:

1. **Write the architecture documents as each stage lands, not in one pass at the end.** Six of the
   seven were promised from stage 0 and written in stage 15. Nothing was wrong with the content, but
   they were reconstructed from code instead of written alongside it, and the scheduling was the
   avoidable part.
2. **Automate the page in CI.** Screenshots prove the UI renders; nothing asserts the approval button
   does what it says. A handful of Playwright tests over the same three interactions as the capture
   script would close the largest testing gap in the front end.
3. **Test concurrency.** The design assumes a conversation has one turn in flight and that the
   approvals table can be read and written from more than one process. Neither is asserted. Given the
   AgentCore target, this is the first thing I would build a test for — two instances against one
   database, one approval, exactly one execution.
4. **Add a live-model smoke run behind a secret.** The suite is deeper than most, but every case runs
   on a deterministic provider. One nightly run against a real model, asserting only that a turn
   completes and picks a sensible tool, would catch prompt and tool-description regressions that the
   scripted provider cannot.
5. **Make every environment variable either read or absent.** Four separate defects in this project
   were variables that were documented and never read: `MCP_DB_PATH` early on, and then
   `ALLOW_RUNTIME_KEY_ENTRY`, `WEB_PORT` and `AGENT_BASE_URL`, which were found only when a question
   about switching models was answered against the code instead of against the docs. Each presented as
   something else — a permissions error, a missing table, a feature described in a runbook that no code
   implements. The first is gone; the other three are now labelled reserved where they are defined. A
   startup assertion that the settings object and `.env.example` agree would have removed a whole class
   of afternoon.
6. **A `forget me` path for memory.** Facts expire; nothing deletes. That is the wrong answer for a
   user who asks, and it is a small feature with a clear contract.
7. **Coverage floor and mutation testing on the two critical modules** — the approval gate and the
   write ledger. They are the parts where an untested branch is a correctness bug rather than a gap.
8. **A second machine and a second Docker version** in the verification, because "works here" is not
   the same claim as "works", and this repository does not currently distinguish them.

## k. On whose thinking this is

The brief asks for my thoughts rather than an LLM's output, and it deserves a direct answer rather
than a footnote.

The code, the tests and the reference documentation in this repository were produced with an AI
assistant in the loop, and section i describes the process in detail, including the controls that
exist because of that. What that means for this README is: the *descriptions* — how the approval
state machine works, which error sentence a model sees, what the last eval run printed — are
mechanically checkable, and every one of them points at a file, a command or a transcript you can open
and disagree with. Nothing in them depends on being trusted.

The judgement calls are the part to weigh, and they are all recorded as decisions with their
alternatives rather than asserted as facts: the ADRs state what was rejected and why (LangGraph, a
framework-owned loop, per-service locks); the commit history states what was taken out of scope and
when; each stage file states what went wrong and what it cost. Where I have said something is
unverified — an ARM64 run, the provenance of a `user_id`, a query against a live `arm64` host, a cost
model — it is because the honest answer was more useful than a confident one, and the reasoning
behind that preference is the same reasoning the rest of this repository runs on: a claim nobody can
check is worth less than a stated limitation.

If I were reviewing this submission, I would read the ADRs and the stage defect logs first, and the
README second — the code is the part that can be regenerated.

---

## Repository layout

```
agentcore-platform/
├── README.md                     you are here — the assignment answers
├── AGENTCORE.md                  the AWS / Bedrock AgentCore design, and what to verify first
├── CONTRACT.md                   the unmodified starter-kit contract for the agent endpoint
├── docker-compose.yml            one command to run everything
├── docker-compose.ci.yml         the CI override (deterministic settings, own compose project)
├── Dockerfile                    the contract's single-container image (agent + MCP + mock)
├── standalone-entrypoint.sh      what that image starts, and the values it repairs
├── Makefile                      thin wrapper over `python -m devtasks`
├── pyproject.toml                uv workspace root; ruff + mypy + pytest config
├── uv.lock                       the reproducibility guarantee
├── devtasks/                     cross-platform task runner, and the CDP screenshot driver
├── eval/                         the 22 cases, the runner, and committed results
├── mock-ticketing/               UNMODIFIED starter-kit fixture
├── services/
│   ├── agent/                    the AgentCore Runtime workload (port 8080)
│   │   └── ui/                   the page, served by the agent itself at `/`
│   └── mcp-ticketing/            the MCP server for tickets (port 8081)
├── tests/                        unit, contract, and integration tests
└── docs/
    ├── plan/                     MASTER-PLAN.md, conventions, and a log per stage
    ├── adr/                      five architecture decision records
    ├── architecture/             overview, data model, tool catalogue, approval FSM,
    │                             memory, observability, failure handling, the mock
    ├── runbooks/                 local dev, evals, providers, troubleshooting
    ├── media/                    the captured run, its eval transcript, and how to regenerate
    ├── agent-platform-assignment/ the task as given, unedited
    └── starter-kit/              the kit's README and sample cases, for provenance
```

`mock-ticketing/`, `CONTRACT.md` and the kit's own compose file came from the starter kit.
`mock-ticketing/` and `CONTRACT.md` are **never edited** — surviving the mock's failures is the
exercise, so the mock stays fragile. That claim is not on the honour system:
[`tests/unit/test_starter_kit_integrity.py`](tests/unit/test_starter_kit_integrity.py) recomputes the
checksums in [`docs/starter-kit/checksums.sha256`](docs/starter-kit/checksums.sha256) and fails if the
fixture changed by a single byte. `docker-compose.yml` **is** ours to extend — it gains the MCP server
and the agent.

## Commands

Run `make help` for the full list with examples. The ones that matter:

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
| `make screenshots` | capture the run into `docs/media/` (`ARGS=--failure` for the fault case) |
| `make up-ci` / `make down-ci` | the CI stack, on its own compose project |
| `make ci` | lint + typecheck + unit tests + secret scan + evals |
| `make clean` | remove caches and local data |

## Where each answer lives

| The brief asks for | Where |
|---|---|
| the plan, and how it was executed | the commit history — one commit per unit of work, each stating what changed and what it cost |
| decisions and their alternatives | [`docs/adr/`](docs/adr/) |
| how the pieces fit, and one request end to end | the module map in `services/agent/src/agent_service/factory.py`, and `tests/contract/test_agent_contract.py` |
| the approval mechanism | `agent/gate.py`, `agent/execution.py`, `db/repositories/approvals.py` |
| tools, their schemas, and the error contract | `services/mcp-ticketing/src/ticketing_mcp/tools.py`, `errors.py` |
| what is stored and for whom | `db/models/`, and the migrations in `services/agent/alembic/` |
| retries, timeouts, giving up honestly | `services/mcp-ticketing/src/ticketing_mcp/errors.py`, `agent/budget.py` |
| tracing, and debugging a run from it | §5 of [`HOW-TO-TEST.md`](HOW-TO-TEST.md), and `observability/tracer.py` |
| the mock, and how to make it fail | [`docs/starter-kit/README.md`](docs/starter-kit/README.md), and the fault-injection flags in §a above |
| the latest eval run, verbatim | [`eval/results/latest.md`](eval/results/latest.md), [`docs/media/eval-run.txt`](docs/media/eval-run.txt) |
| the recorded run, and how to regenerate it | [`docs/media/README.md`](docs/media/README.md) |
| running it locally, and when it breaks | [`HOW-TO-TEST.md`](HOW-TO-TEST.md) |
| the AWS design, and what I would check first | [`AGENTCORE.md`](AGENTCORE.md) |

## Notes for whoever runs this

- **The default model provider needs no key and no network**, so the stack runs, the tests pass and
  the evals pass on a machine with neither. A real provider is selected by configuration — the list
  and the settings that select them are in
  [`config.py`](services/agent/src/agent_service/config.py), and every setting with an example value
  is in [`.env.example`](.env.example).
- **Development happened on Windows and CI runs on Linux.** Everything is verified on both; a path
  that assumes one of them is a bug. That is not a preference — it is where two of the defects in
  this project came from.
- **Secrets never enter the repository or a log.** `.env` is gitignored, `detect-secrets` runs in the
  same form locally and in CI, and the scan has been made to fail on purpose before being trusted.
- **The mock is not ours to fix.** If a test needs different mock behaviour, the test is wrong — or
  it needs a different failure mode, which the mock already offers.

## License

MIT — see [`LICENSE`](LICENSE).

`docs/starter-kit/` is the starter kit's own documentation and its checksum manifest, kept for
provenance and verified by a test on every run. Two directories under `docs/` are published because
they are evidence rather than planning: `docs/media/` (the screenshots and the eval console run) and
`docs/adr/` (the five decision records this README cites). The assignment brief and the working notes
behind this build are not published.
