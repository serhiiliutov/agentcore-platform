# ============================================================================
# ticketing-mcp — the MCP server, as a container.
#
# Build context is the REPOSITORY ROOT, not this directory:
#
#     docker build -f services/mcp-ticketing/Dockerfile -t ticketing-mcp .
#
# The two services share one `uv.lock` workspace, so the lock this image installs
# from has to be the same file the host resolves against. A per-service context
# could not see it, and a service that carried its own lock would drift from the
# workspace the first time a dependency moved — silently, and in the image rather
# than in review.
#
# Two stages, because the build tooling and the runtime have nothing in common.
# The final image carries no uv, no compiler and no dev dependencies, so what a
# reviewer inspects is what actually runs.
# ============================================================================

# --- builder ----------------------------------------------------------------
FROM python:3.12-slim AS builder

# uv is installed from the index this project already depends on, rather than
# copied from its published image, so the build needs no registry beyond PyPI.
# It is deliberately unpinned: what makes the result reproducible is `--frozen`
# installing exactly `uv.lock`, and a floating installer for a tool that only
# performs that install adds a second thing to keep in step for no gain.
RUN pip install --no-cache-dir uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Layer order IS the cache strategy, and it is the reason the manifests are
# copied separately from the source. Every dependency lives in the layer above
# the source, so editing a `.py` file re-runs two steps instead of a full
# dependency sync — which is also what makes the CI build cache worth keeping.
COPY pyproject.toml uv.lock ./
COPY services/agent/pyproject.toml services/agent/pyproject.toml
COPY services/mcp-ticketing/pyproject.toml services/mcp-ticketing/pyproject.toml
# The wheel's own README. `readme = "README.md"` in this member's pyproject is
# metadata hatchling reads while building the package, so omitting it fails the
# build with "Readme file does not exist" *after* the dependency layer has already
# resolved — which reads like a dependency problem and is not one. `.dockerignore`
# excludes `*.md` and re-includes `services/*/README.md` for exactly this reason.
COPY services/mcp-ticketing/README.md services/mcp-ticketing/README.md

# Third-party dependencies only. `--no-install-workspace` keeps this layer free
# of our own code, so it survives every source edit.
RUN uv sync --frozen --no-dev --no-install-workspace --package ticketing-mcp

COPY services/mcp-ticketing/src ./services/mcp-ticketing/src
RUN uv sync --frozen --no-dev --package ticketing-mcp

# --- runtime ----------------------------------------------------------------
# The same base image as the builder, and that is not laziness: `.venv/bin/python`
# is a symlink to the interpreter that created it, and the two stages have to agree
# on where that interpreter lives or the venv is dangling.
FROM python:3.12-slim AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8081 \
    TICKETING_LEDGER_PATH=/data/mcp.sqlite3

WORKDIR /app

# A named user rather than root. The only thing this process writes is its
# idempotency ledger in /data, so an image that can only be compromised into one
# directory is worth the four lines.
#
# This is also why `TICKETING_LEDGER_PATH` above is absolute. The server's default
# ledger path is relative (`data/ticketing-idempotency.sqlite3`), the working
# directory is owned by root, and the result is a startup crash with
# "PermissionError: [Errno 13] Permission denied: 'data'" — which reads like a
# permissions problem and is really a path problem. Pointing the ledger at the one
# directory the app user owns removes the question rather than working around it.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home app \
 && mkdir -p /data \
 && chown app:app /data

COPY --from=builder --chown=app:app /app /app

USER app

EXPOSE 8081

# The check asks the server itself, on the path the agent connects to, rather than
# opening a TCP connection to the port. A socket check passes while the MCP session
# is failing to initialise, which is the failure worth catching.
#
# A status below 500 is a pass, and that is not a cop-out: a bare GET to /mcp is not
# a valid MCP session, so the server is *expected* to answer 4xx. What is being
# measured is that the process is answering at all — a refused connection raises and
# exits non-zero, which is the case this exists for.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import http.client,sys; c=http.client.HTTPConnection('127.0.0.1',8081,timeout=2); c.request('GET','/mcp'); r=c.getresponse(); sys.exit(0 if r.status<500 else 1)"

CMD ["python", "-m", "ticketing_mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8081"]
