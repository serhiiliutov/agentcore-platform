# The recorded run

Five screenshots of the agent working, taken against the running stack, plus the console transcript of the
eval run. The assignment asks for "screenshots or a short recording of one run from start to finish: a
read, a proposed write, the approval, and a failure being handled" and for "the output of your eval run" —
these are those moments.

| File | What it shows |
|---|---|
| [`01-loaded.png`](01-loaded.png) | The chat at <http://localhost:8001> before anything is typed: the model chip in the header naming the provider in force, the conversation list, and the empty transcript. |
| [`02-read.png`](02-read.png) | **A read.** The agent searched for the tickets it was asked about and reported what it found. The `search_tickets` step is expanded, so the arguments it chose and the result it got are both visible. |
| [`03-proposed-write.png`](03-proposed-write.png) | **A proposed write, waiting.** The agent has proposed `create_ticket` and stopped. The arguments that would be sent, the approval id, an expiry, and the two buttons are on screen. **Nothing has been written** — the ticket does not exist yet, which is the point of the frame. |
| [`04-approved-write.png`](04-approved-write.png) | **The approval.** After pressing *Approve and run*: the decision reads `executed`, and the ticket the mock returned is on screen with `"replayed": false`. |
| [`05-failure-handled.png`](05-failure-handled.png) | **A failure, handled.** With the mock failing every request, the tool call comes back `FAILED` and the agent says it could not finish instead of pretending it worked. |
| [`eval-run.txt`](eval-run.txt) | **The eval run**, as console text, recorded verbatim — the 22 cases, their results and the total time. |

Frames `01`–`04` are **one conversation, in order** — that is what makes them "one run from start to
finish" rather than four unrelated pictures. Frame `05` is a second run, because it needs a stack whose
mock is genuinely failing; the reason it cannot be folded into the first run is in
[Why the failure is a separate run](#why-the-failure-is-a-separate-run).

---

## How to reproduce them

Everything below is PowerShell on Windows. On Linux or macOS write `VAR=value` instead of
`$env:VAR='value'`.

### 0. Prerequisites

Docker Desktop running, and `uv` on the host for the chat. From the repository root:

```powershell
cp .env.example .env          # skip if .env already exists; every value works as-is
uv sync --all-groups          # once, for the eval run in step 7
```

**Which model answers, and why it matters for a capture.** The chat's own default is `openai` — see
`services/chat/app.py::CHAT_DEFAULT_PROVIDER` — so with an `OPENAI_API_KEY` in `.env` these frames show a
live `gpt-4o` choosing tools and wording replies, which is the more convincing evidence and the reason the
default is what it is. A live model also means the frames are a *sample*, not a replay: it picks its own
arguments, writes its own ticket title, and may occasionally decide not to call a tool at all. If a run
does something unhelpful, send the message again — and if you want the deterministic behaviour instead
(same tool, same arguments, same wording on every run), pick `scripted` in the settings gear before you
start. That switch is stored, so it holds until the chat is reopened.

### 1. Start the stack and the chat

```powershell
docker compose up -d --wait   # three services, blocks until all are healthy
uv run python -m devtasks chat
```

The first `chat` run resolves a separate environment for the front end and takes about a minute. When it
is serving it prints `Your app is available at http://localhost:8001`. Open that URL.

**If a login screen appears**, login is configured in `.env` (`CHAT_USERS`). Chainlit labels the field
*Email address*, but this app takes the account **name** — type the name from `CHAT_USERS`, not an email
address. Skip this paragraph if the chat opens straight into the conversation; that means `CHAT_USERS` is
unset, which is the default.

**The one thing to get right, on every frame:** each tool call renders as a **collapsed row** that reads
`Used <tool> ⌄`. Click it to expand it before capturing. Collapsed, the frame shows that a tool ran but not
what it was asked or what it answered, which is exactly the evidence these frames exist to provide.

### 2. Frame `01-loaded.png` — the chat, before anything

With the conversation open and empty, capture the browser window. The header should name `openai` (or
`scripted`, if you chose it in step 0).

Save as `docs/media/01-loaded.png`.

### 3. Frame `02-read.png` — a read

In the conversation, send:

```
find tickets about the printer
```

Wait for the reply, then **click the `Used search_tickets` row** to expand it. You should see the
arguments the agent chose, the outcome, and the tickets it found.

Save as `docs/media/02-read.png`.

### 4. Frame `03-proposed-write.png` — a proposed write

In the **same conversation**, send:

```
Create a ticket: the third floor printer is jammed again and two people are blocked.
Requester: Aisha Khan. Priority: high. Category: hardware.
```

The agent proposes a write and stops. A card appears with the exact arguments (`title`, `requester`,
`priority`, `category`, `client_request_id`), an approval id, an expiry, and two buttons: **Approve and
run** and **Decline**.

**Why the fields are in the prompt, and what happens if they are not.** `create_ticket` requires a
requester, a priority and a category, and the agent is instructed not to invent values — so asking only
for "a ticket about the printer" gets the *correct* answer, a question back ("could you please provide the
requester name and let me know the priority level?"), and no card to photograph. That is the guardrail
working, not a defect. Supply the fields in the first message, or answer the question and send a second —
both reach the same card, and the committed frames took the first route.

Before capturing, satisfy yourself that **nothing was created**: open <http://localhost:8000/docs> and
check the ticket list. That is the claim the frame makes.

Save as `docs/media/03-proposed-write.png`.

### 5. Frame `04-approved-write.png` — the approval

Press **Approve and run** and wait for the turn to finish. The chat answers the decision with a message of
its own — `executed — Created ticket TCK-1025.` followed by the ticket JSON — and the id counts up from
the fixture's 24 seeded tickets, so the first one raised on a fresh stack is `TCK-1025`.

Confirm it landed: <http://localhost:8000/docs> now lists it.

Save as `docs/media/04-approved-write.png`.

> **Writing is one-shot per stack, for the deterministic provider.** `scripted` sends a fixed
> `client_request_id`, so a second write against the same stack is refused — correctly, by the MCP
> server's idempotency ledger, with *"This client_request_id was already used for a different request."*
> A live model mints a fresh key each time and can write repeatedly. To reset either way:
>
> ```powershell
> docker compose down -v; docker compose up -d --wait
> ```
>
> That clears the ledger and the seeded tickets together.

### 6. Frame `05-failure-handled.png` — a failure being handled

Stop the stack and bring it back with the mock failing **every** call:

```powershell
docker compose down -v
$env:FLAKY='1'; $env:FLAKY_RATE='1.0'; docker compose up -d --wait
```

Start a **new chat** (the pencil icon, top left), send the same read:

```
find tickets about the printer
```

The turn takes a few seconds — the agent is retrying. Then the `search_tickets` row comes back marked
`FAILED`. **Expand it** to show the sentence the tool returned: the API is rate limiting this client, this
was attempted four times, and it has been given up on rather than retried further. The agent's reply says
it could not finish.

Save as `docs/media/05-failure-handled.png`.

Then put the stack back:

```powershell
docker compose down -v; docker compose up -d --wait
```

### 7. The eval run

```powershell
uv run python -m devtasks eval 2>&1 | Tee-Object docs/media/eval-run.txt
```

It starts its own stack on free ports, runs the 22 cases on the deterministic provider, and ends with a
pass table and a total time. That output **is** the artefact — `Tee-Object` writes it verbatim to
[`eval-run.txt`](eval-run.txt), and the same table is committed as
[`eval/results/latest.md`](../../eval/results/latest.md). There is deliberately no screenshot of a
terminal here: a picture of a console says nothing the console's own text does not, and the text can be
diffed by the next run.

The transcript contains PowerShell's `NativeCommandError` banners interleaved with the real output — that
is not a failure: `uv` logs every HTTP request to stderr and PowerShell wraps anything a native command
writes to stderr in that banner. The command exits 0, and the pass table is the substance. The file is
kept as it happened rather than tidied, so it is evidence rather than a retelling.

Note that the eval run is on `scripted` while the chat frames are on `gpt-4o`, and that is deliberate
rather than an inconsistency: the suite asserts *deterministic tool choices*, so it is a regression gate
only on the provider whose behaviour is written in the repository. The live-model run is committed as
results instead — [`eval/results/live-openai/latest.md`](../../eval/results/live-openai/latest.md).

---

## Why the failure is a separate run

With `FLAKY_RATE=1.0` **every** backend call fails, so the write proposed in frame `03` could never be
executed — the approval in frame `04` would fail and the run would stop half-way. A screenshot of an
approval that cannot succeed would be a picture of the harness, not of the product. The alternative — a
mock that fails occasionally — cannot be relied on to fail during a capture either, and would leave the
most interesting frame present or absent at random.

## The same frames, deterministically

`devtasks screenshots` produces these frames without a browser being driven by hand — see
`devtasks/tasks.py::screenshots`. It is the committed path, and it pins the agent back to `scripted`
first, because a frame named "a proposed write" that photographed whatever a live model happened to say
would be reproducible in name only. Use it when the markup or the chat's behaviour changes and the frames
need regenerating; use this document when you want the frames to show a real model choosing the tools.

## What the images are not

- **Not staged.** Every pixel came from a browser, against the running stack, with the mock's real data
  and the agent's real turns. If a frame looks wrong, the fix is to correct whatever it shows and take it
  again — not to edit the image.
- **Not a recording.** Screenshots, as the assignment permits as an alternative to a recording.
- **Not a claim of determinism.** The chat frames show one sampled run of a live model, so a different
  run would word things differently and might pick different ticket arguments. What is stable is the
  *shape*: a read is executed, a write is proposed and stops, and a human decision is what runs it. The
  deterministic frames are available from `devtasks screenshots`, and the eval suite is what turns the
  behaviour into an assertion.
