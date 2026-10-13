# ADR-0002 — A plain agent loop, and model providers behind one interface

* **Status:** accepted
* **Date:** 2026-10-13
* **Deciders:** Serhii Liutov
* **Related:** ADR-0001 (service boundaries)

## Context

Two decisions that are really one: what runs the agent, and what the model is.

The assignment says any agent framework is fine — LangGraph, Strands Agents, the
OpenAI Agents SDK, or plain code — and any model provider, including a local one.
It also says the provider must be swappable through configuration, and that a
solid, well-engineered basic solution is valued far more than an over-engineered
complex one.

What the work is actually scored on, from the brief:

* state and orchestration that **behave correctly when the service restarts or the
  backend fails**;
* an approval step that **must** happen before a write, and that survives a
  restart;
* memory that is clear about **what is remembered, where, how long and for whom**;
* traces that let us **explain any single run**.

Every one of those is a property of state handling and control flow. None of them
is a property that a graph library provides for free — and a framework that owns
the state machine also owns the place where the bugs will be.

## Decision

### 1. A plain, explicit agent loop, in our own code

Roughly: assemble the prompt → ask the provider for a turn → if the turn contains
tool calls, execute the ones that are allowed to run and append their results →
repeat until the model answers or a budget is exhausted. Plus an interception
layer: a write tool called during the loop is **not** executed, it is recorded as a
pending approval.

The loop is small enough to read in one sitting, and every piece of state it
touches (messages, approvals, facts, spans) lives in a table we can inspect with a
SQL query. When a run misbehaves, the answer is in the trace, not in a framework's
internal checkpoint format.

### 2. Model providers behind one narrow interface

```
ModelProvider.chat(messages, tools, *, budget) -> AssistantTurn
AssistantTurn(text, tool_calls, usage, stop_reason)
```

Six implementations, selected by configuration, per session:

| Provider | Purpose | Needs a key |
|---|---|---|
| `scripted` | deterministic, rule-driven responses | no |
| `replay` | replays a recorded live run | no |
| `anthropic` | Claude, native tool use | yes |
| `openai` | GPT, chat completions with tools | yes |
| `openai_compatible` | **any** OpenAI-shaped endpoint: Ollama, vLLM, LM Studio, Groq, Together | usually no |
| `bedrock` | Amazon Bedrock Converse API | AWS credentials |

`scripted` and `replay` are the default for tests and CI. They are not a
workaround for not having a key — they are what makes the eval suite reproducible
and cheap enough to run on every commit. A live provider is opt-in, per run, and
the results record which one produced them.

### 3. Where a key may come from

Credentials are read from the environment. The web UI may **select** among the
providers that are already configured, and it is told only whether each is
configured — never a key, never a prefix, never a fingerprint.

An opt-in development mode (`ALLOW_RUNTIME_KEY_ENTRY=1`, off by default) lets the
UI submit a key. In that mode the key lives in process memory, is never written to
the database, never logged, never placed in a trace span and never returned by any
endpoint, and it is gone on restart. A test asserts each of those, rather than
trusting the intent.

### 4. Local models

`openai_compatible` is the whole story for local inference: it points at any
OpenAI-shaped endpoint by base URL, which covers Ollama and vLLM with no extra
code. A compose profile brings up Ollama for demonstration.

The honest limitation, measured on the development machine rather than assumed: it
has a 4 GB GPU with no pass-through into Docker on Windows and 16 GB of RAM, so a
local model here means a roughly 3-billion-parameter model on CPU. That is slow
and unreliable at tool selection. It is offered as proof that the provider is
genuinely swappable, and it is never the path the demo or the evals run on. Saying
so is more useful than pretending a local model is equivalent.

## Alternatives considered

**LangGraph.** The strongest candidate. It brings a graph model, checkpointers and
interrupt/resume for human-in-the-loop, which maps directly onto the approval
requirement. Rejected for this project because the approval gate needs to be
*structural* — a write tool that physically cannot run during the model loop,
enforced in the tool executor — and a framework's interrupt primitive is a
cooperative convention instead. We would still have had to build the interception,
the idempotency and the durability, on top of a dependency whose checkpoint
encoding we would then be debugging. It also would have hidden exactly the state
handling the assignment asks us to demonstrate.

**Strands Agents.** Small, AWS-aligned, and would have let `AGENTCORE.md` speak to
a first-party framework. Rejected: it is a thinner layer than LangGraph but still
a layer, and its value here (model abstraction) is the one thing we can write in
about a hundred lines with no dependency risk. Worth revisiting if this became an
AWS-first product.

**OpenAI Agents SDK.** Good ergonomics, but it pulls the design toward
provider-specific primitives, which fights the swappability requirement.

**One provider, hard-coded.** Least code. Rejected: the brief explicitly asks for
swappability, and a deterministic provider is the only way the eval suite can be
both meaningful and free.

## Consequences

**Positive**

* The state the assignment cares about — pending approvals, sessions, facts, spans
  — is in our schema, greppable and queryable, so "why did this run do that?" is
  answerable with SQL.
* Tests and evals run offline, deterministically, with no key and no network.
* Adding a provider is one file and one registry entry.

**Negative**

* We own the loop, the retry budget, the tool-call plumbing and the message
  format conversion for every provider. That is real work, and it is the work this
  project is demonstrating rather than the work being avoided.
* No framework ecosystem to lean on: no prebuilt tool-calling agents, no
  off-the-shelf tracing integration. Mitigated by keeping the loop under 300 lines
  and the trace model under our own control.
* `scripted` and `replay` are code that exists only for testing. They must be
  maintained when the loop changes. They earn that cost by making the eval suite
  runnable in CI and by catching regressions in the orchestration itself, which a
  live-model run cannot do reliably.
