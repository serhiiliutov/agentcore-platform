# ============================================================================
# The single-container image that CONTRACT.md's smoke test builds.
#
#     docker build --platform linux/arm64 -t my-agent .
#     docker run -p 8080:8080 --env-file .env my-agent
#
# WHY THIS FILE EXISTS AT ALL. `CONTRACT.md` opens with that command, and until this
# existed there was no `Dockerfile` at the repository root — so the first thing a
# reviewer would try failed with `open Dockerfile: no such file or directory` before
# any of the work was visible. The service images live at
# `services/agent/Dockerfile` and `services/mcp-ticketing/Dockerfile` and are what
# `docker compose up` uses; this one is the *contract's* shape, and it is deliberately
# a different trade: one process tree per container instead of one service per
# container.
#
# WHAT IT PUTS IN ONE IMAGE, and why each part is here rather than assumed:
#
#   * the agent service, on :8080 — the thing the contract is about;
#   * the MCP server, launched over stdio by the agent, because a second container
#     would not be reachable from a bare `docker run -p 8080:8080`;
#   * the ticketing mock, started by the entrypoint on 127.0.0.1:8000, because
#     `CONTRACT.md`'s `--env-file .env` carries no ticketing host and a smoke test
#     that needs three commands is not the smoke test that was documented.
#
# The task's own topology — one service per container, one `docker compose up` — is
# unchanged and is still what the README's quickstart uses. This image is a
# convenience for the documented smoke test, and the entrypoint says in its log when
# it overrides a compose-only setting, so a reviewer can see what happened rather
# than guess.
# ============================================================================

# --- builder ----------------------------------------------------------------
FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Manifests first, source later: this layer holds every third-party dependency and
# survives every source edit. Both members are named, so the lock installed here is
# the lock the workspace resolves against.
COPY pyproject.toml uv.lock ./
COPY services/agent/pyproject.toml services/agent/pyproject.toml
COPY services/mcp-ticketing/pyproject.toml services/mcp-ticketing/pyproject.toml
# Each member's own README, because `readme = "README.md"` is metadata hatchling reads
# while building the wheel. `.dockerignore` excludes `*.md` and re-includes exactly
# these, so this copy is possible — see the note there.
COPY services/agent/README.md services/agent/README.md
COPY services/mcp-ticketing/README.md services/mcp-ticketing/README.md
RUN uv sync --frozen --no-dev --no-install-workspace \
    --package agent-service --package ticketing-mcp

COPY services/agent/src ./services/agent/src
COPY services/mcp-ticketing/src ./services/mcp-ticketing/src
RUN uv sync --frozen --no-dev --package agent-service --package ticketing-mcp

# Runtime assets of the repository rather than of either package: the Alembic
# configuration and the migration directory. The agent image explains at length why
# the schema is migrated rather than created (`services/agent/docker-entrypoint.sh`).
COPY services/agent/alembic.ini ./services/agent/alembic.ini
COPY services/agent/alembic ./services/agent/alembic

# The mock, from the fixture. Its three files — plus the requirements file the kit
# ships — are un-ignored in `.dockerignore` for this image alone; the fixture itself is
# not modified, which `tests/unit/test_starter_kit_integrity.py` keeps true.
COPY mock-ticketing/requirements.txt ./mock-ticketing/requirements.txt
RUN uv pip install --no-cache --python /app/.venv/bin/python -r ./mock-ticketing/requirements.txt
COPY mock-ticketing/app.py mock-ticketing/seed.py ./mock-ticketing/

COPY standalone-entrypoint.sh ./standalone-entrypoint.sh

# --- runtime ----------------------------------------------------------------
# The same base image as the builder: `.venv/bin/python` is a symlink to the
# interpreter that created it, and both stages have to agree on where that is.
FROM python:3.12-slim AS runtime

# The defaults are the *standalone* ones: stdio MCP, the embedded mock, and a data
# directory inside the image's volume. An `--env-file` overrides any of them, which is
# the documented way and also the way two of them can arrive wrong — see the
# entrypoint, which repairs those two and logs it.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AGENT_HOST=0.0.0.0 \
    AGENT_PORT=8080 \
    MCP_TRANSPORT=stdio \
    MCP_STDIO_COMMAND="python -m ticketing_mcp" \
    MCP_EXPECTED_SERVER_NAME=ticketing \
    TICKETING_BASE_URL=http://127.0.0.1:8000 \
    TICKETING_API_KEY=dev-key-123 \
    TICKETING_LEDGER_PATH=/data/mcp.sqlite3 \
    DATABASE_URL=sqlite+pysqlite:////data/agent.sqlite3 \
    TRACE_DIR=/data/traces

WORKDIR /app

# Non-root, with /data as the only writable place — the same contract as the service
# images: the database, the WAL files beside it, the idempotency ledger and the traces
# all live in the one directory the volume is mounted on.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home app \
 && mkdir -p /data/traces \
 && chown -R app:app /data

COPY --from=builder --chown=app:app /app /app

# The executable bit cannot be relied on from the build context: a Windows checkout
# hands Docker a file with no mode to preserve, and the image would then fail at start
# with "permission denied" rather than at build.
RUN chmod +x /app/standalone-entrypoint.sh

USER app

EXPOSE 8080

# `/ping` is the contract's own health route, so this checks the thing the platform
# checks. `HealthyBusy` counts as a pass — see the agent image's own healthcheck.
HEALTHCHECK --interval=10s --timeout=3s --start-period=40s --retries=5 \
  CMD python -c "import http.client,sys; c=http.client.HTTPConnection('127.0.0.1',8080,timeout=2); c.request('GET','/ping'); r=c.getresponse(); sys.exit(0 if r.status==200 else 1)"

ENTRYPOINT ["/app/standalone-entrypoint.sh"]
