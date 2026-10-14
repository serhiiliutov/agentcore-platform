# How to test this application

Everything you need to run this by hand: start it, open it, send it messages, and watch what it
writes to the database while you do. Written for Windows PowerShell; on Linux or macOS replace
`$env:X='y'` with `X=y`.

Nothing here needs an API key, a model account or a network connection. The default model provider
is a deterministic one that runs locally (`scripted`), so the same message always produces the same
behaviour.

**Read this before judging the answers.** `scripted` is not a language model. It matches your
prompt against a short, ordered list of rules
(`services/agent/src/agent_service/providers/scripted.py`) and returns the tool call those rules
name — that is what makes the 22 eval cases reproducible and free to run on every commit. So its
prose is canned (*"Done. I used 1 tool call(s) to answer."*) and it will not reason about a question
no rule covers. **That is the provider, not the architecture:** the agent is built against a
provider interface with six implementations, and pointing it at a real model is one environment
variable and a container recreate — `AGENT_MODEL_PROVIDER=anthropic` with `ANTHROPIC_API_KEY` set,
see §6 and README section c. The page's header says which provider is answering
(`model: scripted - deterministic, no key needed`), so the two are never confused, and hovering it
says how to switch.

**What is genuinely rule-based and what is not.** The tool choice, the approval gate, the error
handling, the retry policy, the idempotency ledger and the memory extraction are ordinary code, and
they behave identically whichever provider is in force — that is the part this assignment is about,
and it is tested directly. Only the choice of *which* tool a sentence calls and the wording of the
reply come from the provider, and only there does a real model change the result.

---

## 0. What you need

| | |
|---|---|
| **Required** | [Docker Desktop](https://www.docker.com/products/docker-desktop/), running. Nothing else. |
| **Optional** | Python 3.12 + [uv](https://docs.astral.sh/uv/) — only for the automated tests in §6 |
| **Optional** | A SQLite GUI for §5: DB Browser for SQLite, or the VS Code SQLite Viewer extension |

Check Docker is up before anything else:

```powershell
docker info --format 'server={{.ServerVersion}}'
```

If that errors, start Docker Desktop and wait for the whale icon to stop animating.

> **There is no `make` on a stock Windows shell.** This project's `Makefile` is a convenience wrapper
> for people who have it. Every command in this document is the real one, so you never need `make` —
> and where a `make` name is mentioned, the equivalent is `uv run python -m devtasks <name>`.

---

## 1. Start it

From the repository root:

```powershell
cp .env.example .env              # every value in it already works
docker compose up -d --wait       # builds and starts three services; blocks until healthy
docker compose ps                 # expect three services, all (healthy)
```

`--wait` is the whole check: it exits non-zero if any service's healthcheck never passes, so you do
not have to poll anything. The three services are:

| Service | Port | What it is |
|---|---|---|
| `agent` | **8080** | the application — the HTTP API, the model loop, the approval gate, the page |
| `mcp-ticketing` | 8081 | the tool server the agent reaches the ticketing system through |
| `ticketing` | 8000 | the mock ticketing API (the fixture, unmodified) |

A first check from the terminal, without a browser:

```powershell
Invoke-RestMethod http://localhost:8080/ping
# status
# ------
# Healthy
```

To stop it, keeping the data (`docker compose stop`), or stopping and deleting the containers
(`docker compose down`), or also deleting the database (`docker compose down -v` — everything the
agent learned is gone).

To watch the agent work while you use the page, in a second terminal:

```powershell
docker compose logs -f agent      # Ctrl+C stops watching; it does not stop the service
```

---

## 2. Open the page

```powershell
start http://localhost:8080/       # or just open that URL in any browser
```

The page is served by the agent itself — there is no separate front-end service to start.

What you are looking at:

| Element | What it is |
|---|---|
| **Your name** (top left) | the `user_id` every request is scoped to. Memory is per user, so changing it starts a clean slate |
| **Model** (top left) | which provider is answering — `scripted` (deterministic rules, no key) or the model you configured. Hover it for how to change it |
| **Session** (top right) | the conversation id. The same value resumes the same conversation; the box is editable, so you can type an old id to go back to it |
| **New session** (top right) | empties the transcript and starts a fresh conversation. It deletes nothing: the previous conversation stays in the database under its own session id |
| **Message box** | type here and press **Send** (or Enter) |
| **Transcript** | the conversation, oldest at the top, with every tool call the agent made shown as a row under the reply it belongs to: tool name, arguments, outcome, how long it took |
| **Approval card** | appears when the agent wants to *write* something. It shows the exact tool and arguments, and has **Approve** / **Decline** |

Nothing is written to the ticketing system until you press **Approve** on a card. That is the point
of the whole exercise.

---

## 3. Messages to send, and what to expect

Send these in the order given — the approval flow needs two turns, and the memory example needs two
sessions. Every row below was run to check it, with the default provider.

### 3.1 A read (no approval needed)

| Send | What you should see |
|---|---|
| `find tickets about the printer` | A tool row for **search_tickets** with `{"q": "printer"}`, marked executed, its result summarised as *"1 of 1 matching ticket(s) on page 1."*, then the answer **Done. I used 1 tool call(s) to answer.** No approval card: reading needs no permission. |
| `What is the status of TCK-1002?` | A tool row for **get_ticket** with `{"ticket_id": "TCK-1002"}`, and the ticket's title, status and comment count in the summary. Try another id, e.g. `TCK-1042`, to see a not-found handled: the tool row is marked **failed** and the answer says it gave up rather than pretending. |
| `find tickets about the vpn` | `search_tickets` with `{"q": "vpn"}` — the same path with a different subject. |

### 3.2 A write (needs your approval)

| Send | What you should see |
|---|---|
| `open a ticket for the flickering monitor` | A tool row for **create_ticket** marked **proposed** — *nothing has been created yet* — and an **approval card** showing the exact arguments (`title`, `requester`, `priority`, `category`, `client_request_id`) and an expiry time. The reply says it needs your approval. |
| then click **Approve** on the card | The card disappears, and a tool row appears with the same arguments marked **executed**, with the summary *"Created ticket TCK-1025."* (the id counts up from the seed's 24 tickets). **The ticket now exists** — you can see it at `http://localhost:8000/docs`, or by asking `What is the status of TCK-1025?` |

> **Run this once per clean stack.** The deterministic provider always sends the same
> `client_request_id`, so a *second, differently-worded* write on the same stack is refused — correctly,
> by the MCP server's idempotency ledger, with *"This client_request_id was already used for a
> different request."* That is the ledger doing its job, not a fault: it is what guarantees one write
> per approval. To run the write examples again from scratch:
>
> ```powershell
> docker compose down -v && docker compose up -d --wait
> ```
>
> `down -v` clears the ledger and the seeded tickets together. (Reading, declining and the memory
> examples can be repeated freely — only *writing* is one-shot per stack.) If you have already run
> `make eval`, do this before §3.2: the suite creates tickets too.

### 3.3 A write you refuse, and one you answer with a message

| Send | What you should see |
|---|---|
| `open a ticket for the broken keyboard` then click **Decline** | The card is marked declined. The ticketing system was never called — the ticket count is unchanged. |
| `open a ticket for the broken keyboard` then, instead of the button, send `yes, go ahead` | The same result as clicking Approve: the next message in the same session *is* the approval. This is the flow `CONTRACT.md` specifies. |
| `open a ticket for the broken keyboard` then send `no thanks` | Nothing is created. |
| `open a ticket for the broken keyboard` then send `what's the weather in Paris?` | The question is answered normally and **the proposal stays pending** — a card is still on screen, because asking something unrelated is not a decision. You can still approve it afterwards. |

### 3.4 Memory (two sessions)

| Send | What you should see |
|---|---|
| `I'm Priya Raman on the Platform team` | Answered in general terms — the fact is stored silently. |
| click **new session** (or change the session id), then send `what do you know about me?` | **Here is what I have on file:** followed by `- team: Platform` and `- requester_name: Priya Raman`. This is cross-session memory for the same `user_id`. |
| change **your name** at the top left to something else, then ask `what do you know about me?` | *"I do not have anything remembered about you yet."* — memory is per user and one user never sees another's. |

### 3.5 A turn that runs out of time (optional)

Only reproducible with a short budget, because the default is 90 seconds:

```powershell
$env:AGENT_REQUEST_DEADLINE_S = '1'
docker compose up -d agent
```

Then send `make the turn take longer than the turn budget` — the turn stops and says it ran out of
time instead of hanging. Set the variable back to `90` and recreate the agent afterwards.

### 3.6 What the agent will *not* do

Ask it something outside IT support — `who won the world cup?` — and it calls no tool. Be aware of
what you are observing here: with the built-in deterministic provider the reply is a plain
acknowledgement of your words, whereas with a real model (see `README.md`, section c) it declines in
its own words. Either way it does not go looking for tools it does not have, and it never invents a
ticket.

---

## 4. What the API looks like without a browser

Same conversation, from a terminal — useful when you want to see the raw JSON. Two turns of one
session:

```powershell
$h = @{ 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id' = 'try-1' }

# 1. a read: the tool runs immediately
Invoke-RestMethod http://localhost:8080/invocations -Method Post -Headers $h -ContentType application/json `
  -Body (@{ prompt = 'find tickets about the printer'; user_id = 'me' } | ConvertTo-Json) |
  Select-Object stop_reason, tool_calls

# 2. a write: proposed, not executed
Invoke-RestMethod http://localhost:8080/invocations -Method Post -Headers $h -ContentType application/json `
  -Body (@{ prompt = 'open a ticket for the flickering monitor'; user_id = 'me' } | ConvertTo-Json) |
  Select-Object stop_reason, pending_approval

# 3. answering in the same session is the approval
Invoke-RestMethod http://localhost:8080/invocations -Method Post -Headers $h -ContentType application/json `
  -Body (@{ prompt = 'yes, go ahead'; user_id = 'me' } | ConvertTo-Json) |
  Select-Object stop_reason, tool_calls
```

The three `stop_reason` values you should see are `completed`, `awaiting_approval`, `completed`.
`pending_approval` carries the approval id, the tool name and the exact arguments that will run.

The other way to decide, using the id from step 2 — this is what the page's Approve button calls:

```powershell
Invoke-RestMethod http://localhost:8080/invocations/approvals/<approval_id>/decide -Method Post `
  -ContentType application/json -Body (@{ user_id = 'me'; approved = $true } | ConvertTo-Json)
```

---

## 5. Watch the database in real time

The database is SQLite, so any SQLite GUI can read it. There is one wrinkle: by default it lives
**inside a Docker volume**, which is not a file Windows can open. The fix is to run the agent with
that directory bind-mounted onto the host, so the file is an ordinary file on disk.

### 5.1 Start the stack with the database on the host

```powershell
docker compose -f docker-compose.yml -f docker-compose.dbview.yml up -d --build --wait
```

That override replaces the agent's `/data` volume with `./data/dbview/` on this machine, so:

```
data\dbview\agent.sqlite3          ← the agent's database (163 KB after one message)
data\dbview\traces\<id>.jsonl      ← one trace file per request, one per turn
```

SQLite may also create `-wal` and `-shm` sidecar files beside the database while it is writing. They
come and go, and a GUI does not need them; open the `.sqlite3` file and nothing else.

(The MCP server's own ledger stays in its volume; it is not interesting to watch.)

Check it exists:

```powershell
Get-ChildItem data\dbview
```

Send one message on the page (e.g. `find tickets about the printer`), then look again: `agent.sqlite3`
will have grown and a trace file will have appeared under `traces\`. That growth *is* the agent
persisting the conversation. Checked rather than assumed, by sending one message and reading the
result back **from outside the container**: one session, two messages, one trace row.

### 5.2 Open it in a GUI

**Option A — DB Browser for SQLite** (free, recommended):

```powershell
winget install -e --id DBrowserForSQLite.DBBrowserForSQLite    # if --id fails, search: winget search "DB Browser"
```

Then: **Open Database** → `D:\agentcore-platform\data\dbview\agent.sqlite3` → the **Browse Data**
tab → pick a table. Press **F5** (or re-select the table) after each message you send on the page to
see new rows. Useful tables, in the order they fill up:

| Table | What appears as you use the page |
|---|---|
| `sessions` | one row per session id, created on the first message |
| `messages` | every turn: your prompt, the assistant's answer, and tool results, with a `sequence` that orders them |
| `approvals` | one row per proposal — `state` goes `pending` → `executed`/`declined`/`failed`, with the exact `payload` that ran |
| `traces` | one summary row per request: duration, status, provider, tool-call count, token counts |
| `user_facts` | what it remembered about you: `key`, `value`, `expires_at` |

**Option B — VS Code**: install the **SQLite Viewer** extension (`qwtel.sqlite-viewer`), open
`data/dbview/agent.sqlite3` in the editor, and re-select the file after each message.

**Option C — no GUI at all**, genuinely live (this is the same read a GUI performs):

```powershell
while ($true) {
  uv run python -c "import sqlite3;c=sqlite3.connect('data/dbview/agent.sqlite3');print(c.execute('select id,state,action from approvals order by requested_at').fetchall());print('messages:',c.execute('select count(*) from messages').fetchone()[0])"
  Start-Sleep 2
}
```

Watch the `approvals` state change from `pending` to `executed` **live** while you press Approve in
the browser — that is the assignment's central guarantee, visible as a database row.

Handy read-only queries to paste into the GUI's **Execute SQL** tab:

```sql
-- the last few turns of a conversation
SELECT sequence, role, substr(content, 1, 60) AS content FROM messages ORDER BY sequence DESC LIMIT 20;

-- every proposal, and what became of it
SELECT action, state, requested_at, decided_at, executed_at, result FROM approvals ORDER BY requested_at DESC;

-- how long turns are taking, and whether any failed
SELECT started_at, duration_ms, status, tool_calls, total_tokens FROM traces ORDER BY started_at DESC LIMIT 10;

-- what the agent remembers about people
SELECT user_id, key, value, expires_at FROM user_facts;
```

### 5.3 Two things to know before you write to it

- **Open it read-only while the agent is running.** The agent is writing to the same file; SQLite
  allows concurrent readers but a GUI edit takes a write lock, and in WAL mode a reader always sees
  the last committed state — so reading is safe and live, writing is not.
- **`make reset`** (or `uv run python -m devtasks reset`) resets the *ticketing mock's* fixture, not
  this database. To start the agent's own state over: `docker compose down -v` and start again.

### 5.4 Going back to the normal setup

```powershell
docker compose -f docker-compose.yml -f docker-compose.dbview.yml down
docker compose up -d --wait          # back to the named volume
```

---

## 6. The automated tests (optional)

These need Python 3.12 and `uv`, not Docker:

```powershell
uv sync --all-groups                                                    # once
uv run python -m devtasks test-unit                                     # 446 tests, ~10s, no stack
uv run python -m devtasks eval                                          # 22 cases, starts its own stack, ~45s
uv run python -m devtasks test-integration                              # 4 tests, needs the stack from §1
```

`eval` is the interesting one: it drives the real HTTP API through 22 scenarios (right tool chosen,
approval respected, declined writes nothing, failure handling, memory, timeouts) and writes its
report to `eval/results/latest.md`. Their `make` equivalents are `make test-unit`, `make eval`,
`make test-integration`, for anyone who has GNU Make.

---

## 7. When something is wrong

| Symptom | Cause and fix |
|---|---|
| `docker compose up` fails, or `docker info` errors | Docker Desktop is not running. Start it and wait for the icon to settle. |
| `port is already allocated` | Something else holds 8080, 8081 or 8000. `docker compose down`, then check `docker ps`. |
| The page loads but a message hangs | The agent is waiting on the MCP server. `docker compose logs mcp-ticketing agent` shows which side is stuck. |
| `/ping` answers but the page is blank | Hard-refresh (Ctrl+F5) — the page is served fresh by the agent, and an old cached copy can mismatch the API. |
| A message comes back `I have nothing to work from yet.` | The message was empty or only whitespace. Type something. |
| You want a clean slate | `docker compose down -v` then `docker compose up -d --wait` — containers and data both recreated. |
