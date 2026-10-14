# ============================================================================
# agent-service — the AgentCore Runtime workload, as a container.
#
# Build context is the REPOSITORY ROOT, not this directory:
#
#     docker build -f services/agent/Dockerfile -t agent-service .
#
# Same reason as the MCP image: one `uv.lock` workspace, so the lock installed
# here is the lock the host resolves against. See services/mcp-ticketing/Dockerfile
# for the longer version of that argument.
#
# The image migrates before it serves — see docker-entrypoint.sh for why that is
# not merely convenient. It needs `alembic.ini` and the migration directory at
# runtime, which are build artefacts of this repository rather than of the wheel,
# so they are copied explicitly.
# ============================================================================

# --- builder ----------------------------------------------------------------
FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Manifests first, source later: this layer holds every third-party dependency
# and survives every source edit.
COPY pyproject.toml uv.lock ./
COPY services/agent/pyproject.toml services/agent/pyproject.toml
COPY services/mcp-ticketing/pyproject.toml services/mcp-ticketing/pyproject.toml
# The wheel's own README, because `readme = "README.md"` in this member's pyproject
# is metadata hatchling reads while building the package — see the note in
# `.dockerignore`, which excludes `*.md` and re-includes `services/*/README.md` so
# this copy is possible at all.
COPY services/agent/README.md services/agent/README.md
RUN uv sync --frozen --no-dev --no-install-workspace --package agent-service

# The package itself. `python -m agent_service` finds it in the venv copied below;
# `alembic`'s `prepend_sys_path = src` also puts the same source on the path, which
# is what lets a migration import the models it is migrating.
COPY services/agent/src ./services/agent/src
RUN uv sync --frozen --no-dev --package agent-service

# Runtime assets of the repository rather than of the package: the Alembic
# configuration and the migration directory. Both are needed at run time, and
# neither is inside the wheel hatch builds from `src/agent_service`.
COPY services/agent/alembic.ini ./services/agent/alembic.ini
COPY services/agent/alembic ./services/agent/alembic
COPY services/agent/docker-entrypoint.sh ./services/agent/docker-entrypoint.sh

# --- runtime ----------------------------------------------------------------
# The same base image as the builder: `.venv/bin/python` is a symlink to the
# interpreter that created it, and both stages have to agree on where that is.
FROM python:3.12-slim AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AGENT_HOST=0.0.0.0 \
    AGENT_PORT=8080 \
    DATABASE_URL=sqlite+pysqlite:////data/agent.sqlite3 \
    TRACE_DIR=/data/traces \
    MCP_TRANSPORT=http \
    MCP_HOST=mcp-ticketing \
    MCP_PORT=8081

WORKDIR /app

# Non-root, with /data as the only writable place. Every consumer of this image
# reads that as the contract: the database, the WAL files beside it and the traces
# all live in the one directory the volume is mounted on.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home app \
 && mkdir -p /data/traces \
 && chown -R app:app /data

COPY --from=builder --chown=app:app /app /app

# The executable bit cannot be relied on from the build context: a Windows
# checkout hands Docker a file with no mode to preserve, and the image would then
# fail at start with "permission denied" rather than at build.
RUN chmod +x /app/services/agent/docker-entrypoint.sh

USER app

EXPOSE 8080

# `/ping` is the contract's own health route, so this checks the thing the platform
# checks. It answers 200 with `{"status":"Healthy"}` — and also with `"HealthyBusy"`
# while a turn is in flight, which is a pass: a working service under load is not a
# broken one, and the contract exists precisely so a supervisor can tell them apart.
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import http.client,sys; c=http.client.HTTPConnection('127.0.0.1',8080,timeout=2); c.request('GET','/ping'); r=c.getresponse(); sys.exit(0 if r.status==200 else 1)"

# The entrypoint migrates and then execs the server, so there is no CMD to add.
ENTRYPOINT ["/app/services/agent/docker-entrypoint.sh"]
