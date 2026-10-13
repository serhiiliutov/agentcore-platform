# Stage 7 — Model providers behind one interface

* **Status:** complete
* **Started:** 2026-10-13
* **Estimate:** agent 5–7 h · human 2 days
* **Depends on:** Stage 2 (settings), Stage 6 (the service skeleton)
* **Branch:** `main`

## Goal

Six providers behind the one method ADR-0002 fixes — `chat(messages, tools, *, budget) ->
AssistantTurn` — with two of them needing no credential at all, so the whole project runs and
the evals are reproducible with nothing configured.

## Deliverables

| Module | Responsibility |
|---|---|
| `providers/base.py` | The neutral types, the `ModelProvider` protocol, `build_provider` |
| `providers/scripted.py` | Deterministic, rule-driven; the default |
| `providers/replay.py` | Reproduces a recorded run; catches what a rule author cannot imagine |
| `providers/anthropic_provider.py` | Messages API; content blocks, system-as-parameter |
| `providers/openai_provider.py` | Chat completions; also every OpenAI-shaped local endpoint |
| `providers/bedrock_provider.py` | Converse; synchronous SDK, so dispatched to a thread |
| `tests/unit/test_providers.py` | 31 tests, including the hygiene ADR-0002 demands |

## The three translation problems, solved once

All three live in `base.py` rather than in each provider, which is why the loop never mentions
a vendor:

| Vendor says | Neutral form | Why it matters |
|---|---|---|
| Anthropic: `input` is a mapping; OpenAI: `arguments` is a JSON **string** | `ToolCallRequest.arguments` is parsed | the loop never parses JSON it did not expect to |
| Anthropic/Bedrock: system is a **parameter**; OpenAI: a role | `Message(role="system", …)`, extracted per provider | one conversation shape for the loop |
| `tool_use` / `tool_calls` / `stopReason` | `StopReason` in our own three values | a vendor spelling never reaches the loop |

## Definition of done

| Check | Command | Result |
|---|---|---|
| Lint is clean | `python -m devtasks lint` | **pass** — `All checks passed!` |
| Formatting is clean | `python -m devtasks fmt` | **pass** — no diff |
| Types are clean | `python -m devtasks typecheck` | **pass** — `Success: no issues found in 56 source files` |
| All tests | `python -m devtasks test-unit` | **pass** — `197 passed` |
| A bare checkout runs with no key | `test_the_default_provider_is_scripted_and_needs_no_credential` | **pass** |
| A missing credential fails at startup | `test_an_unconfigured_provider_is_refused_before_it_is_used` | **pass** |
| The neutral types cannot carry a secret | `test_the_neutral_types_cannot_carry_a_secret` | **pass** |
| A settings repr leaks no key | `test_a_settings_repr_does_not_contain_the_key` | **pass** |
| The provider list leaks no key material | `test_configured_providers_leaks_no_key_material` | **pass** |
| A replay never wraps around | `test_exhausting_a_recording_raises_rather_than_restarting` | **pass** |

## Findings worth recording

* **A test caught a real bug, not a design.** `_query_from` returned the first word over three
  characters, so `"find tickets about the printer"` searched for **`about`** — a term the API
  accepts and nothing matches. Every scripted search would have looked empty, quietly hollowing
  out the eval path it exists to exercise. A filler set now excludes request-words from the
  subject, and a test asserts the meaningful term comes back. This is the argument for the
  deterministic provider in one line: a scripted run *can* be asserted against, so a defect
  like this surfaces in CI rather than as an eval that mysteriously proves nothing.
* **boto3 is synchronous.** There is no async Bedrock client, so the call is dispatched with
  `asyncio.to_thread`. Calling it directly would block the event loop for a whole model round
  trip — meaning `GET /ping` stops answering while a turn runs and the runtime sees a *dead*
  instance rather than a busy one. A real failure mode, and the reason the dispatch is in the
  provider rather than left to the caller.
* **One legitimate type suppression.** boto3 ships no type information, so its import carries
  `type: ignore[import-untyped]` with a comment. That is a gap in the dependency, not an
  inconvenience; everything done with the client is still checked against the returned shapes.
* **The key is optional for OpenAI when a base URL is set.** A local endpoint usually needs
  none, and demanding a dummy secret from an operator running Ollama would be friction with no
  security benefit.
* **`replay` is not a second `scripted`.** `scripted` *manufactures* a conversation from rules;
  `replay` *reproduces* one a real model produced. Only the second can prove the loop handles
  what a model actually emits.

## Result

Six providers, one interface, 197 tests, and a stack that runs end to end with no credential.
The hygiene claim is enforced by tests over the types rather than asserted in prose, and the
one bug found this stage was found by a test written to check behaviour rather than to confirm
a decision.

## Carried forward

* **Stage 8** installs the loop as the injected `InvocationHandler`. It calls `chat` and gets
  an `AssistantTurn`; nothing in it will mention a provider.
* **The scripted provider's write path is the eval's route to the approval gate** — it proposes
  `create_ticket` with a fixed `client_request_id`, which is what Stage 4's ledger and Stage 8's
  gate both key on.
