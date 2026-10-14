# Runbook: model providers

Six providers, selected by configuration, all behind one interface. The default needs no key and no
network, so **nothing on this page is required to run the stack, the tests or the evals**. Read it
when you want a real model answering.

## The default: nothing to do

```bash
AGENT_MODEL_PROVIDER=scripted
```

`scripted` is a rule-driven provider that produces deterministic tool calls. It is not a stub: the
eval suite, the screenshots and the CI gates all depend on it being exactly reproducible, and it is
what makes a failure in the orchestration distinguishable from a failure in a model. Its companion
`replay` replays a recorded live run the same way.

## What each provider needs

| Provider | Needs | Notes |
|---|---|---|
| `scripted` | nothing | the default; deterministic |
| `replay` | nothing | replays a recorded run |
| `anthropic` | `ANTHROPIC_API_KEY` | Claude, native tool use |
| `openai` | `OPENAI_API_KEY` | chat completions with tools |
| `openai_compatible` | `OPENAI_BASE_URL` | any OpenAI-shaped endpoint — Ollama, vLLM, LM Studio, Groq, Together |
| `bedrock` | `AWS_REGION` (+ AWS credentials) | Amazon Bedrock Converse API |

A provider is offered by the page **only if it has what it needs**; the check is
`Settings.configured_providers()`. The page is told which providers are configured and never a key,
a prefix, or a fingerprint.

## Switching provider

The settings are read once, when the process starts, so a change means a restart:

```bash
# .env
AGENT_MODEL_PROVIDER=anthropic
AGENT_MODEL_ID=                      # empty = the provider's own default model

docker compose up -d agent           # recreate the agent with the new environment
```

Two things to know about `.env`, because both behave differently from what people expect:

- **Compose reads `.env`; the application does not.** `Settings` is constructed with
  `env_file=None` on purpose — the shell and compose own the environment. So editing `.env` does
  nothing at all for `make serve` or `uv run python -m eval`; export the variable instead:
  `$env:AGENT_MODEL_PROVIDER='anthropic'` on Windows, `AGENT_MODEL_PROVIDER=anthropic` on Linux.
- **A missing key for the selected provider is a clear startup error**, not a confusing failure
  three turns in: `selected_provider_is_usable()` is checked while building the application.

There is also a runtime switch. The page can change the provider for the session, which writes a row
to `provider_settings` (`scope` = `global` or `session:<id>`) rather than to a process variable —
a row, so the choice survives a restart and a reviewer can see it.

## Per provider

### Anthropic

```bash
ANTHROPIC_API_KEY=sk-ant-...
AGENT_MODEL_PROVIDER=anthropic
AGENT_MODEL_ID=            # or e.g. a specific Claude model id
```

### OpenAI

```bash
OPENAI_API_KEY=sk-...
AGENT_MODEL_PROVIDER=openai
```

### OpenAI-compatible (local models)

One base URL covers every OpenAI-shaped endpoint. With Ollama on the host:

```bash
OPENAI_BASE_URL=http://host.docker.internal:11434/v1
OPENAI_API_KEY=ollama               # most local servers ignore it but require a value
AGENT_MODEL_PROVIDER=openai_compatible
AGENT_MODEL_ID=llama3.2
```

`host.docker.internal` is the host as seen **from inside a container**, on Docker Desktop. From a
host-run agent (`make serve`) use `http://127.0.0.1:11434/v1` instead. That difference is the single
most common reason a local model "cannot be reached" while the server is plainly running.

The honest limitation, measured rather than assumed: on the machine this was developed on — a 4 GB
GPU with no pass-through into Docker on Windows, 16 GB RAM — a local model means roughly a
3-billion-parameter model on CPU. It is slow and unreliable at tool selection. It exists to
demonstrate that the provider interface is genuinely swappable, and it is never the path the demo or
the evals run on.

### Amazon Bedrock

```bash
AWS_REGION=eu-central-1
BEDROCK_MODEL_ID=                  # empty = the region's default for the configured model
AGENT_MODEL_PROVIDER=bedrock
```

Credentials come from the standard chain — environment variables, a shared config profile, or an
instance role. **Inside a container they must be passed explicitly**, because there is no instance
role and no host `~/.aws` unless you mount one:

```bash
docker compose run --rm -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN agent
```

That the provider is behind the same interface as the others is the point of the AgentCore design in
[`AGENTCORE.md`](../../AGENTCORE.md): on Bedrock AgentCore the runtime supplies the credentials and
this configuration disappears.

### Replay

```bash
AGENT_MODEL_PROVIDER=replay
```

Replays a recorded run, which is how a live-model failure can be turned into a reproducible test.

## Keys: where they may live, and where they may not

- **In the environment**, which is the normal path for a deployment.
- **In process memory**, only when `ALLOW_RUNTIME_KEY_ENTRY=1` and only for a development run. In that
  mode the page may submit a key; it is held in memory, is **never** written to the database, never
  logged, never placed in a trace span and never returned by any endpoint, and it is gone on restart.
  Never enable it on anything internet-facing.

Every credential setting is a Pydantic `SecretStr`, so a `repr()` of the settings — in a log line, an
exception, a trace attribute or a debug print — shows `**********` instead of the key. Making that
the type's job means it cannot be forgotten at one call site. A test plants a canary key in the
environment and asserts it appears in no provider request and no response.

## Verifying that it took effect

```bash
make doctor                        # what is installed, configured and answering
curl -s localhost:8080/ping        # {"status":"Healthy"}

curl -s -X POST localhost:8080/invocations -H 'Content-Type: application/json' \
  -d '{"prompt":"find tickets about the printer","user_id":"me"}'
```

Then confirm which model actually answered, from the trace row rather than from the reply:

```sql
SELECT provider, model_id, model_turns, total_tokens, duration_ms
FROM traces ORDER BY started_at DESC LIMIT 1;
```

`provider` should name what you configured. If it still says `scripted`, the process did not pick up
the environment — most often because `.env` was edited while a host-run process was reading the
shell.

## Failure modes

| Symptom | Cause |
|---|---|
| Startup error naming the provider | it is selected but has nothing it needs — the check is deliberate |
| `openai_compatible` cannot be reached | `host.docker.internal` from a container, `127.0.0.1` from the host |
| `bedrock` fails with no credentials | inside a container the chain has no profile or role; pass the variables |
| A key appears nowhere but the provider still says `scripted` | `.env` was edited but the process reads the shell (see above) |
| Tool selection is erratic | a small local model. The tool descriptions are written for a capable model; a 3B model on CPU is not one. Switch to a hosted provider and re-run |

For anything else, [`troubleshoot.md`](troubleshoot.md).
