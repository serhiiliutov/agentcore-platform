# Runbook: troubleshooting

Symptom first, because that is what you have when you open this page. Each entry names the symptom,
the cause, and the command that confirms it. Everything here is a failure this project actually hit
during development, or one its own configuration makes easy to reach.

## Start here

```bash
docker compose ps                       # which services are up, and whether they are healthy
make doctor                             # docker, python, ports, and what the agent answers
docker compose logs --tail=50 agent     # the service you suspect
docker compose logs --tail=50 mcp-ticketing
```

`make doctor` is the fastest single command: it checks the tooling, whether the daemon is answering,
whether the three ports are free, and what each service reports.

## The stack will not come up

### `error during connect: ... docker daemon is not running`

Docker Desktop is not running. Start it and wait for the whale to stop animating — `docker compose`
fails immediately otherwise, which is what you are seeing. This is the first thing to check on a
freshly booted machine.

```bash
docker info > $null; echo $LASTEXITCODE     # 0 means the daemon is answering
```

### `port is already allocated` / `bind: address already in use`

Something holds 8000, 8080 or 8081. The usual culprit is **the other stack**: the development stack
and the CI stack publish the same ports and cannot run at the same time.

```bash
make down && make down-ci               # stop both, then start the one you want
docker compose ps --all                 # anything still Up from an earlier run
```

On 8000 specifically, other software commonly claims it. Find the owner:

```bash
# Windows
Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object OwningProcess
Get-Process -Id <pid>
# Linux/macOS
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

### `--wait` never returns, or a service stays `starting`

`--wait` is honest: it waits for each **healthcheck**, and a healthcheck passes only when the
application answers on its own route, not merely when the port is bound. So a service stuck in
`starting` is an application that is not up. Ask it directly:

```bash
docker compose logs --tail=80 mcp-ticketing   # the usual culprit: it starts last
docker compose exec ticketing python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').read())"
docker compose exec agent python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/ping').read())"
```

The dependency order is deliberate — `agent` waits for `mcp-ticketing`, which waits for `ticketing`,
because the agent's lifespan connects to the MCP server and verifies the tool catalogue. A failure in
the first service therefore looks like a failure in the last one.

### The image build fails with `Readme file does not exist: README.md`

`.dockerignore` excludes `*.md` to keep documentation out of the image, and each service's
`pyproject.toml` declares `readme = "README.md"` — which **hatchling reads while building the wheel**.
An over-broad pattern therefore fails the build *after* the dependency layer has resolved, so it
reads like a dependency problem and is not one.

The file keeps `!README.md` and `!services/*/README.md` as explicit negations for exactly this
reason: un-ignoring makes the file available to `COPY`, and the Dockerfile copies it. If you add a
service, add its README to that negation list.

### The mock's image or an agent startup fails under `/data`

A path problem wearing a permissions costume. If a service dies with `PermissionError` on a file
under `/data`, check the variable name before the filesystem:

```bash
docker compose exec mcp-ticketing printenv TICKETING_LEDGER_PATH
```

The MCP server reads **`TICKETING_LEDGER_PATH`**. `.env.example` documented `MCP_DB_PATH` for a
while — a variable **nothing reads**. Setting only that leaves the server on its *relative* default
(`data/ticketing-idempotency.sqlite3`) under a root-owned working directory, and it dies at startup
with an error that names permissions rather than the path. The same class of mistake: a variable
that is documented, plausible, and inert.

## The stack is up and the agent misbehaves

### `GET /ping` answers, but every invocation fails with `503` and "still starting up"

The app's lifespan has not finished connecting to the MCP server. Give it a second, or ask why:

```bash
docker compose logs --tail=40 agent
```

The usual cause is the MCP handshake: `MCP_TOKEN` must be **the same value on both sides**, because
both read one variable. A mismatch fails the `initialize` call, which is deliberate — a token
mismatch should not surface later as a mysterious tool error.

### Every tool call fails with `Authentication` and "not a retry"

`TICKETING_API_KEY` (on the MCP server) must equal the mock's `API_KEY`. From `.env`, both come from
one variable, so a mismatch means something set one of them directly:

```bash
docker compose exec mcp-ticketing printenv TICKETING_API_KEY
docker compose exec ticketing printenv API_KEY
```

### The agent answers but never calls a tool

Check the trace before the code:

```sql
SELECT id, provider, model_id, model_turns, tool_calls, status FROM traces ORDER BY started_at DESC LIMIT 1;
```

- `provider = scripted` and `tool_calls = 0` — the scripted provider matches prompts by rule. Ask a
  question its rules cover (see `eval/cases.json` for the phrasings the suite uses), or configure a
  real provider ([`provider-setup.md`](provider-setup.md)).
- `provider = scripted` and the whole turn failed — read `error`, then the trace file named by
  `trace_path`.

### Every write is refused, or a proposal vanished

```bash
docker compose exec agent sqlite3 /data/agent.sqlite3 \
  "select id, action, state, datetime(expires_at), datetime(decided_at) from approvals order by requested_at desc limit 5;"
```

- `expired` — `AGENT_APPROVAL_TTL_S` (default 1800) passed before anyone decided. Raise it, or decide
  sooner. An expired proposal is refused with a **409**, and that refusal is a correct answer rather
  than a fault.
- `pending` but invisible in the page — the page lists proposals on load from
  `GET /api/approvals?user_id=…`; a different `user_id` will not see it, by design. One user can
  never see or decide another's proposal.
- `approved` and nothing happened — the decision was recorded and the execution did not complete.
  That is the one state with no automatic recovery (see
  [`../architecture/approval-state-machine.md`](../architecture/approval-state-machine.md)); the
  trace's `execution` span says what the backend answered.

### A ticket exists that the agent said it failed to create

This is the case the design exists to survive, and it is not a bug. A write whose response was lost is
reported as `Unresolved` and **is not retried**, because the ticket may exist. Confirm with the
ledger, which ties the two together:

```bash
docker compose exec mcp-ticketing sqlite3 /data/mcp.sqlite3 "select * from idempotency_keys;" 
docker compose exec ticketing python -c "import urllib.request;print(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/tickets',headers={'X-API-Key':'dev-key-123'})).read())"
```

The `client_request_id` is the join: one key, one ticket, however many times the call was attempted.

### The second eval run behaves differently from the first

A write that was answered from a previous run's ledger, so no ticket was created. The eval suite sets
`TICKETING_LEDGER_PATH` into a per-run directory for exactly this reason; a *host* run of the agent
does not, and will reuse `data/ticketing-idempotency.sqlite3`. Clear it, or point it somewhere
throwaway:

```bash
make clean                       # host-side data/, including the ledger
TICKETING_LEDGER_PATH=$(mktemp -d)/ledger.sqlite3 make serve
```

### Chaos is still on from a previous experiment

`FLAKY=1` in your shell exports to every container compose starts, and it is easy to leave set after a
failure-path experiment. Every tool call then fails a share of the time:

```bash
docker compose exec ticketing printenv FLAKY FLAKY_RATE FLAKY_SEED
docker compose up -d ticketing            # with FLAKY unset in the shell
```

`FLAKY_SEED` is what makes the failures reproducible; without it, the same run can pass twice and
fail once.

## The dev loop on the host

### Tests pass, `make serve` fails with "no such table"

The database path is **relative**, so the process's working directory decides which file it opens.
Run from the repository root and you get a different, empty database. `make serve` sets the working
directory to `services/agent/` for this reason, and so does `devtasks serve`:

```bash
make migrate                     # from the repository root; runs alembic with the right cwd
```

If you set `DATABASE_URL` yourself, give it a full SQLAlchemy URL. The settings reject anything
without a scheme at startup rather than failing on the first query:
`sqlite+pysqlite:///./data/agent.sqlite3`.

### Editing `.env` changes nothing

Two separate reasons, and they are worth knowing apart:

- **Host-run processes never read `.env`.** `Settings` is constructed with `env_file=None`; the shell
  owns the environment. Export the variable instead
  (`$env:AGENT_MODEL_PROVIDER='anthropic'` / `AGENT_MODEL_PROVIDER=anthropic`).
- **Containers read `.env` only when compose resolves it**, which happens when they are *created* —
  not when they restart. `docker compose up -d agent` recreates; `docker compose restart agent` does
  not.

### `make` is not recognised

GNU Make is optional and often absent on Windows. Every target is one line delegating to the task
runner, so:

```bash
uv run python -m devtasks lint
uv run python -m devtasks test-unit -k approval -x
uv run python -m devtasks up-ci
```

`make help` and `python -m devtasks --help` list the same commands.

### PowerShell prints scary red banners from `make`

PowerShell wraps anything a program writes to **stderr** in a `NativeCommandError` block, and `uv`
and Docker both write ordinary progress there. The command's real result is in `$LASTEXITCODE`:

```bash
uv run python -m devtasks typecheck 2>&1 | Select-Object -Last 3; "EXIT=$LASTEXITCODE"
```

This is why the committed eval transcript in
[`../media/eval-run.txt`](../media/eval-run.txt) contains that chatter, and why
[`../media/README.md`](../media/README.md) says so rather than tidying it: it is what the run
actually printed.

### A variable I documented seems to do nothing

Check whether anything reads it before believing it:

```bash
grep -rn "WEB_PORT" --include=*.py .       # in this case: no reader exists
```

Two variables in `.env.example` belong to a container that was never built (`WEB_PORT`,
`AGENT_BASE_URL` for a separate nginx UI). The page is served by the agent at `/` on 8080, so those
two are inert. Three separate defects in this project were variables that were documented and never
read, each presenting as something else entirely — which is why the setting is now: if a variable
cannot be shown to change behaviour, it is documented as inert rather than left to disappoint
somebody.

## When you need to see it fail, not fix it

```bash
FLAKY=1 FLAKY_RATE=1.0 docker compose up -d ticketing && make screenshots ARGS=--failure
LATENCY_MS=5000 AGENT_REQUEST_DEADLINE_S=10 make serve     # force the turn to run out of time
```

Both produce a user-visible, honest failure rather than a hang, and the way to confirm that is the
message the page shows plus the trace, in
[`../architecture/observability.md`](../architecture/observability.md).

## Escalation checklist

Before concluding something is wrong with the code, confirm all six:

1. `docker info` exits 0.
2. `docker compose ps` shows three services, all healthy.
3. `make doctor` reports the ports free before starting, and the endpoints answering after.
4. `FLAKY` is unset in the shell that started the stack.
5. The trace row for the run says which provider answered and whether the turn completed.
6. The failure reproduces on a clean slate: `docker compose down -v && make up`.
