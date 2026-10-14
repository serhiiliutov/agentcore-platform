# Memory: what is remembered, where, for how long, for whom

The assignment asks for clarity on four questions about memory. This document answers each with the
column or the rule that enforces it, and then states the two things the implementation deliberately
does *not* do — because the interesting part of a memory design is usually its refusals.

There are two kinds of memory in this system, and they are separate on purpose:

| | Short-term | Long-term |
|---|---|---|
| What | the conversation so far | one durable fact about a person |
| Where | `sessions` + `messages` tables | `user_facts` table |
| How long | the session; never swept | `expires_at`, from `AGENT_MEMORY_FACT_TTL_DAYS` |
| For whom | one user, one session | one user, across all their sessions |
| Loaded by | `SessionRepository` as history | `FactRepository` into the system prompt |

## Long-term memory, question by question

**What.** A single scalar `key` with a string `value` — not a blob, not a transcript. The whole
vocabulary is three keys, named in one tuple so the extractor, the renderer and the tests cannot
disagree:

```
FACT_KEYS = ("requester_name", "team", "preferred_category")
```

If something cannot be expressed as "a name for one piece of information", it does not belong in
this table. That constraint is what keeps memory inspectable and deletable rather than an opaque
store, and it is the reason the answer to "what do you know about me?" is a `SELECT`:

```sql
SELECT key, value, source_session_id, expires_at FROM user_facts WHERE user_id = ?;
```

**Where.** In the same database as everything else. A reviewer can list exactly what is remembered
about a person without reading application code, which is the point of choosing a table over an
embedding store.

**How long.** `expires_at`, set from configuration at write time and **evaluated on read** — it is
part of the read index, so an expired fact is never loaded and then filtered. The default is
`AGENT_MEMORY_FACT_TTL_DAYS`. Expiry rather than deletion: a fact that has stopped applying is kept,
so a reviewer can see what was once believed and when it stopped being true. A "forget me"
hard-delete path does not exist, which is a gap — named in [`AGENTCORE.md`](../../AGENTCORE.md)
rather than glossed.

**For whom.** `user_id`, indexed, and enforced by the repository layer. **There is no query in this
codebase that reads a fact without it.**

`source_session_id` records which conversation taught us the fact. That is easy to omit and
expensive to add later, and it is what makes "why do you think that about me?" answerable precisely.
Provenance is a feature, not metadata.

## How a fact is learned, and the rule that bounds it

Extraction is **deterministic regexes over the user's own prompt** (`agent/memory.py`), not a model
call. Three consequences, each of which is the reason for the choice:

- The keys are a fixed whitelist, so the only thing a model extractor could add is a second way to be
  wrong.
- It is reviewable and testable without a provider, which is what lets an eval assert on it.
- It is identical on every run, so a failing case is reproducible.

The bound that matters most: **only the user's prompt is read** — not the assistant's reply, not a
tool result, not a ticket body. That is the whitelist's real job. The danger in remembering is not a
missed fact; it is a ticket description or a quoted email becoming durable state about a person.
Reading one field with three known keys is a rule that cannot leak, while "read the conversation and
remember what looks important" is a rule that cannot be checked.

Two small correctness details, both of which were bugs first:

- `"I'm on the B team"` matches the *name* pattern as well as the team pattern. A stopword list
  (`on`, `in`, `the`, `looking`, …) plus a rule that a team match suppresses the name reading for the
  same phrase is what keeps a name field from filling with sentence fragments.
- `"the B team"` captures `"B team"`; the word "team" belongs to the sentence, not the value, and is
  dropped. There is a test asserting exactly this, because the first version of the pattern claimed
  to handle it and did not.

## How a fact is used

Injection happens **in the system prompt, per turn, and nothing is written back to the transcript**:

```
What you know about this user from earlier conversations:
- team: Platform
```

Four properties follow, and each is the reason for the design rather than a side effect:

- The stored turn is the prompt the user typed, so the conversation the next turn reads is the one
  that actually happened, not one with a memory preamble spliced into it.
- A fact that expires stops appearing **without anything having to be deleted or rewritten** — the
  block is derived each turn.
- `render()` returns an empty string when there is nothing to say, rather than a heading with nothing
  under it. A model told to expect facts that are not there will sometimes supply them.
- Cross-session recall is therefore observable over HTTP: the `memory_across_sessions` eval case
  speaks in a first session and asks in a second, and the scripted provider's "what do you know" rule
  reads the injected block. That is how an eval can assert on memory with no live model.

## Isolation, and the honest limit of the guarantee

`test_agent_memory.py` asserts that one user's facts are invisible to another, and the
`memory_isolation_between_users` eval case asserts it end to end. What that proves is **isolation**:
the queries are scoped and no unscoped read path exists.

What it does not prove is **provenance**. The `user_id` arrives in the request body, so a client that
lies about it sees another user's memory. Locally I cannot fix that — there is no identity provider
in a `docker compose` stack — and pretending otherwise would be the wrong kind of confidence. On
AgentCore the caller's identity from Identity would make isolation an authentication property rather
than a convention, which is item 6 in the verification table of [`AGENTCORE.md`](../../AGENTCORE.md).

## What memory deliberately is not

- **Not a preference store for the agent itself.** Nothing the agent learns about *how to behave*
  is persisted; that is configuration, and mixing the two would make the fact table a place where
  behaviour changes invisibly.
- **Not a summary of past conversations.** No turn is summarised into memory. Summaries are
  lossy in ways nobody can review, and a fact store with three keys and an expiry is a promise a
  user can actually hold the system to.
- **Not shared between users on a team.** Two people on the same team each have their own
  `user_id` and their own facts. Sharing is a plausible feature and a genuinely different design —
  it needs an authorization model, not a foreign key.
