# Stage 14 — Deployment polish and CI

**Status: complete.** Verified locally on Windows 11 / Docker 26.1.1 (`amd64`) and by a
`linux/arm64` cross-build of both images. Every command in this file was run; every number in it
is copied from output, not recalled.

- **Estimate:** Agent 2–3 h · Human 1–1.5 days
- **Deliverables (plan):** multi-stage Dockerfiles, non-root users, healthchecks on all four
  services, `--platform linux/arm64` documented, `.dockerignore`s, complete `docker-compose.yml`
  + `docker-compose.ci.yml`, `Makefile` targets, `.github/workflows/ci.yml`
  (lint → typecheck → unit → build → integration → eval → secret scan), build-cache layer
  ordering with `uv sync --frozen`.
- **DoD (plan):** `docker compose up -d` brings all four services to healthy with a single
  command; `make ci` reproduces the pipeline locally; an ARM64 build succeeds via buildx.

---

## What was built

| Artefact | What it is |
|---|---|
| `services/agent/Dockerfile` | Two-stage image. The builder installs dependencies with `uv`; the runtime is non-root (user `app`, uid/gid 10001) and carries a healthcheck. |
| `services/mcp-ticketing/Dockerfile` | The same shape for the MCP server, with `/data` owned by `app` so the idempotency ledger is writable. |
| `services/agent/docker-entrypoint.sh` | Applies Alembic migrations, then `exec`s the service — `exec` so SIGTERM reaches uvicorn's lifespan shutdown. |
| `docker-compose.yml` | The development stack: `ticketing`, `mcp-ticketing`, `agent`, ordered by `depends_on: condition: service_healthy`, with named volumes for both data directories. |
| `docker-compose.ci.yml` | The CI *override*: deterministic settings on top of the development file. See “The CI file is an override, not a second stack”. |
| `.github/workflows/ci.yml` | Six jobs: `static`, `test`, `eval`, `secrets`, `images`, `arm64`. |
| `devtasks/tasks.py`, `devtasks/__main__.py`, `Makefile` | `up-ci`, `down-ci`, `ps-ci` — the CI stack became reachable from a developer's terminal. |
| `.secrets.baseline` | The reviewed allow-list for the secret scan. |
| `.dockerignore`, `.env.example` | Corrected; both had defects that broke the build or misled a reader. |

### Layer ordering, and why it is written the way it is

The dependency layer is built before any source is copied, so editing a service does not
re-install its dependencies:

```
COPY uv.lock pyproject.toml ./
COPY services/agent/pyproject.toml ./services/agent/
COPY services/mcp-ticketing/pyproject.toml ./services/mcp-ticketing/
RUN uv sync --frozen --no-dev --no-install-workspace --package agent-service   # deps only
COPY services/agent/src ./services/agent/src
RUN uv sync --frozen --no-dev --package agent-service                          # the package itself
```

`--no-install-workspace` is the flag that makes the first `RUN` stop at the dependency set;
without it `uv` tries to build a package whose source is not in the image yet. `--frozen` makes
the image fail rather than silently resolve something different from `uv.lock`.

**Both images build from the repository root.** The two services share one `uv.lock`, which is
what makes `--frozen` mean anything across services; a per-service build context could not see
it, and a per-service lock would drift. The cost is a larger context, mitigated by
`.dockerignore`.

### Non-root, deliberately

The runtime stage creates `app` at uid/gid 10001 rather than using the base image's default user,
and gives it ownership of exactly the two directories it writes to (`/app` and `/data`). The MCP
server's ledger is a SQLite file and the agent's entire state is SQLite plus JSONL traces, so both
write to `/data` and nothing else needs to be writable.

---

## Decisions

### The CI file is an override, not a second stack

The first version used Compose's `include:`. It looked like the tidy answer — one topology, one
place for the pins — and Compose rejects it outright:

```
services.agent conflicts with imported resource
```

A service defined in the main file cannot adjust one the included file brought in. So
`docker-compose.ci.yml` is an **override fragment** applied on top of the development file, which
is the mechanism that exists for exactly this:

```
docker compose -f docker-compose.yml -f docker-compose.ci.yml -p agentcore-platform-ci up -d --build --wait
```

Three consequences worth stating:

- **`-p agentcore-platform-ci`.** Without a separate project name both stacks are project
  `agentcore-platform`, so `up-ci` would *replace* a developer's running containers and share
  their volumes. With it, they coexist and their databases are separate files.
- **No Compose-version check in the workflow.** The check existed to guard `include:`'s 2.20
  requirement. The override pair is as old as Compose itself, so the check was deleted rather than
  kept as decoration.
- **No `!reset` / `!override` tags.** The second attempt used them to unpublish the mock's and the
  MCP server's ports in CI. They are far newer than the override mechanism they would be used
  inside, so the file would have failed on any Compose older than about 2.24 — in exchange for a
  cosmetic gain, since nothing else on a CI runner wants ports 8000 or 8081.

### The CI stack is reachable from a developer's terminal

`docker-compose.ci.yml` existed and **nothing invoked it**: `devtasks/tasks.py` had a
`_compose(..., ci=True)` branch no command reached, and the filename appeared only in a guard that
raised if it was missing. A stack only CI can start is a stack only CI can debug, so `up-ci`,
`down-ci` and `ps-ci` were added, and `up-ci` uses `--wait` — a broken stack then fails there
rather than in the test that follows it.

### Three services, not four

The plan says “healthchecks on all four services” and “all four services to healthy”. There are
three: the web page is served by the agent at `/` on `:8080`, not by a separate `nginx` container
on `:3000`. This is the Stage 11 deviation, recorded there and repeated here because a reviewer
counting services will look in this file first. The reasoning is in ADR-0001: the assignment
scores agent engineering, and keeping the page inside the agent keeps the image a pure AgentCore
Runtime workload. Every service that exists has a healthcheck.

### Chaos stays off in CI

`FLAKY=0`, `FLAKY_RATE=0.0` and `LATENCY_MS=0` are pinned in `docker-compose.ci.yml`. A pipeline
that fails two runs in five is a pipeline people learn to re-run without reading, which trains
everyone to ignore it. The failure paths are not abandoned — they are covered where they can be
asserted on, by `tests/integration/test_failure_paths.py` (which creates the failure it asserts
about) and by the eval suite's deterministic modes. The same reasoning is in the workflow header.

### Secrets are scanned with `detect-secrets`, not the gitleaks Action

`detect-secrets` is already a pinned dev dependency, so the check that runs in CI is byte-for-byte
the check a developer runs locally, on Windows, with no extra install. The gitleaks Action would
need a licence secret on an organisation account and a third-party action otherwise. The trade is
recorded in the workflow header so the next person can make the opposite call knowingly.

---

## Defects found while building this

Every one of these was a real failure, found by running the thing rather than by reading it. Two
of them were checks that could not fail, which is the failure mode this stage was supposed to
guard against elsewhere.

### 1. `.dockerignore` broke the build it was meant to speed up

The first `docker build` failed in the MCP builder:

```
error: Readme file does not exist: README.md
```

`.dockerignore` excluded `*.md`, and each service's `pyproject.toml` declares
`readme = "README.md"` — so the package could not be built at all. Fixed by re-including
`services/*/README.md` after the exclusion. A `*.md` exclusion looks free until something reads
one.

### 2. `MCP_DB_PATH` — a documented variable nothing reads

`mcp-ticketing` exited 1 at startup:

```
PermissionError: [Errno 13] Permission denied: 'data'
```

The cause was not permissions. The variable was named `MCP_DB_PATH` in the compose file and in the
README, and **no code reads it**: the server reads `TICKETING_LEDGER_PATH`
(`services/mcp-ticketing/src/ticketing_mcp/config.py`). Unset, it fell back to its relative default
under a root-owned `/app`, which produces a permissions error only in the sense that the wrong
directory is unwritable. Fixed in three places — the Dockerfile's default, the compose file, and
`.env.example`. This is the third instance in this project of a variable that is documented but
never read, and the third time it presented as something other than itself.

### 3. The workflow asserted a value the contract never returns

The smoke step I wrote asserted `"stop_reason":"end_turn"`. The smoke test I then *ran* returned:

```
stop_reason: completed
```

`end_turn` is the loop's internal name; the handler translates it for the wire. As written the job
could never pass — a check that looked green in review and was guaranteed red in practice. It now
asserts the contract's own vocabulary, and asserts both halves: `"stop_reason":"completed"` **and**
`"outcome":"executed"`. Either alone would pass while the other was broken, because a turn can
complete having called nothing, and a call can be attempted and refused.

### 4. The secret scan could not fail

`detect-secrets scan` exits 0 whether or not it finds anything — its job is to *write* a baseline,
not to gate on one. The job as written would have passed with a live credential in the tree, and
been cited afterwards as evidence that it had been checked. `devtasks secrets` now reads the JSON
back, compares each finding's hash against `.secrets.baseline`, and fails with a listing.

### 5. The secret scan did not look at new files

With the gate finally able to fail, a planted credential still passed. The scan reported the same
seven findings and none of them was the probe, because **`detect-secrets scan` without
`--all-files` scans only git-tracked files**, as its own help says:

```
--all-files           Scan all files recursively (as compared to only scanning git tracked files).
```

Every file it reported was tracked. A secret in a brand-new file — the way secrets actually arrive
— was invisible, and in CI, where the tree is a checkout, it would have looked fine too. The flag
is now always passed.

### 6. `--all-files` alone scans the environment, and the exclusion that was meant to fix it matched nothing

`--all-files` reports 147 files, because it also walks `.venv`, `.git` and local artifacts whose
findings are third-party noise that would fail every run. The exclusion pattern was written as
`[/\\]\.venv[/\\]…` — anchored on a separator — and a top-level directory is walked as
`.venv/Lib/...`, with nothing in front of it. So it matched nothing, the run took 193 seconds, and
the output still listed `.venv\Lib\site-packages\sqlalchemy\...`. It looked like a gate doing its
job. The leading separator is now optional (`(^|[\\/])`), and the same pattern excludes
`.secrets.baseline` itself, whose recorded `hashed_secret` hex strings otherwise read as secrets
and made the gate fail on a clean tree.

### 7. Two `_compose` branches, one of them a lie

`_compose(ci=True)` picked a single file and raised `"docker-compose.ci.yml does not exist yet
(added in Stage 14)"` when it was missing — a message about a stage rather than about the problem,
for a mode no caller used. It now takes the pair and the project name, and its failure message says
the file is missing.

### 8. The CI file was written and nothing could reach it

Covered under “The CI stack is reachable from a developer's terminal”. It is listed here as well
because it is the same class of defect as Stage 11's unreachable web page: artefacts that pass
their own tests while being absent from the running system.

---

## Verification

Each command below was run in this order, on this machine.

### The development stack

```
$ docker compose up -d --build --wait
EXIT=0
SERVICE         STATUS
agent           Up 11 seconds (healthy)
mcp-ticketing   Up 22 seconds (healthy)
ticketing       Up 4 minutes (healthy)
```

`--wait` is the whole DoD in one command: it blocks until every healthcheck passes and exits
non-zero if one never does, so “all services healthy” is one exit code rather than a poll and a
hope. A turn through the containers, agent → MCP → mock:

```
$ curl -s localhost:8080/ping
{"status":"Healthy"}

$ curl -s -X POST localhost:8080/invocations -H 'Content-Type: application/json' \
    -d '{"prompt":"find tickets about the printer","user_id":"smoke","session_id":"smoke-1"}'
{
    "output":  "Done. I used 1 tool call(s) to answer.",
    "tool_calls": [ { "name": "search_tickets", "outcome": "executed",
                      "summary": "1 of 1 matching ticket(s) on page 1." } ],
    "stop_reason": "completed",
    "steps": 2
}
```

### The CI stack, from a developer's terminal

```
$ uv run python -m devtasks down          # the dev stack holds the same host ports
$ uv run python -m devtasks up-ci
$ uv run python -m devtasks ps-ci
NAME                                     STATUS                    PORTS
agentcore-platform-ci-agent-1            Up 35 seconds (healthy)   0.0.0.0:8080->8080/tcp
agentcore-platform-ci-mcp-ticketing-1    Up 47 seconds (healthy)   0.0.0.0:8081->8081/tcp
agentcore-platform-ci-ticketing-1        Up 53 seconds (healthy)   0.0.0.0:8000->8000/tcp
```

Separate project, separate containers, separate volumes — and the same two assertions the workflow
makes, against this stack:

```
$ curl -s localhost:8080/ping
{"status":"Healthy"}
# a turn: output "Done. I used 1 tool call(s) to answer.", stop_reason completed,
#         search_tickets -> executed
```

### The gate

```
$ uv run python -m devtasks format     → All checks passed! (no files reformatted)
$ uv run python -m devtasks lint       → All checks passed!
$ uv run python -m devtasks typecheck  → Success: no issues found in 100 source files
$ uv run python -m devtasks test-unit  → 376 passed, 4 deselected in 14.51s
```

`test_devtasks_cli.py` covers the command table, so the three new commands are checked by the same
test that would have caught a mis-registered one.

### The secret gate, proven able to fail

A gate is only worth its line in the workflow if it can go red, so it was made to. A file was added
containing AWS's own published example key id — a value the `AWSKeyDetector` matches — and the task
was run:

```
$ uv run python -m devtasks secrets
$ detect-secrets.exe scan --all-files --exclude-files (^|[\\/])(\.venv|\.git|data|…)[\\/]|(^|[\\/])\.secrets\.baseline$
1 unreviewed finding(s):
  temp_secret_probe.py:5  [AWS Access Key]
task failed: the secret scan found 1 value(s) not in .secrets.baseline. If one is a real
credential, remove it from the tree and rotate it. …
EXIT=1
```

Exactly one finding, and it was the planted one. The probe was then deleted and the scan re-run:

```
$ uv run python -m devtasks secrets
secrets: nothing new — 3 reviewed value(s) in .secrets.baseline, 8 finding(s), all recognised
EXIT=0
```

The eight are the reviewed values: the starter kit's `dev-key-123` (in the dev tasks, the eval
stack, the MCP config and three test modules), the `sk-CANARY-must-never-appear` string that
`tests/unit/test_providers.py` asserts never reaches a log or a response, and the env-var *name*
that matches a keyword rule. `.secrets.baseline` was regenerated with the same flags afterwards
(5 077 bytes) and the scan stayed green.

Note the path in the failure message: had this gate been left as the first version, it would have
reported success and been quoted as evidence. That is what defects 4–6 above are about.

### linux/arm64

Both images were cross-built on this `amd64` host with QEMU emulation, so the `RUN` steps really
executed for `arm64` rather than being skipped:

```
$ docker buildx build --platform linux/arm64 -f services/mcp-ticketing/Dockerfile \
      --output type=cacheonly .
#16 [builder 10/10] RUN uv sync --frozen --no-dev --package ticketing-mcp
#16 53.97       Built ticketing-mcp @ file:///app/services/mcp-ticketing
#17 [runtime 4/4] COPY --from=builder --chown=app:app /app /app
#17 DONE 1.6s                                    (452s total)

$ docker buildx build --platform linux/arm64 -f services/agent/Dockerfile \
      --output type=cacheonly .
#19 [builder 13/13] COPY services/agent/docker-entrypoint.sh ./services/agent/docker-entrypoint.sh
#20 [runtime 4/5] COPY --from=builder --chown=app:app /app /app
#21 [runtime 5/5] RUN chmod +x /app/services/agent/docker-entrypoint.sh
#21 DONE 0.5s                                    (328s total)
```

`--output type=cacheonly` builds without exporting: the local daemon is `amd64` and cannot load a
foreign-platform image, and nothing here needs to run one.

**What this proves:** every dependency has an `aarch64` wheel or builds from source, and the
Dockerfiles are architecture-independent.
**What it does not prove:** that the stack *runs* on `arm64`. That would need an `arm64` host or a
full emulated run, and claiming it from a build is exactly the unsupported claim `CONTRACT.md`
asks us to state honestly instead.

---

## Definition of done

| DoD item | Result |
|---|---|
| `docker compose up -d` brings every service to healthy with one command | **Met.** `--wait` exit 0; `agent`, `mcp-ticketing` and `ticketing` all `(healthy)`. |
| `make ci` reproduces the pipeline locally | **Met, with one stated limit.** `devtasks ci` runs lint → typecheck → unit → secrets → eval. The workflow additionally builds the images, starts the stack, smoke-tests it and cross-builds for `arm64`. Each of those is reachable locally (`up`, `test-integration`, `up-ci`, and the `buildx` commands above) but needs Docker, so `ci` does not chain them. |
| An ARM64 build succeeds via buildx, or the limitation is stated | **Met.** Both images cross-build for `linux/arm64`; running on `arm64` is stated as unverified rather than claimed. |
| Healthchecks on all four services | **Deviation, recorded above.** Three services exist, because the page is served by the agent (Stage 11, ADR-0001). Every service that exists has a healthcheck. |
| Secret scan in CI | **Met, and it can fail.** Defects 4–6 are the three ways it could not, all found by planting a credential and watching. |

### What a reviewer should check first

1. `docker compose up -d --wait` → three healthy services, one exit code.
2. `uv run python -m devtasks secrets` → green; then add any file with a `sk-`-shaped string and
   watch it go red.
3. `git log --oneline -1` after this commit, and `docs/plan/stage-14.md` against the working tree:
   the defects above are the substance of this stage, not the Dockerfiles.
