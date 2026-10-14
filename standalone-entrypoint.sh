#!/bin/sh
# ============================================================================
# Entrypoint for the single-container image (`Dockerfile` at the repository root).
#
# It does four things in order, and each has a reason it cannot be skipped:
#
#   1. **Repairs up to three environment values that only make sense inside
#      `docker compose`.** They arrive here when a reviewer follows `CONTRACT.md`
#      literally — `docker run --env-file .env my-agent` — with the `.env` the README
#      tells them to create (`cp .env.example .env`). Each repair is logged, so the
#      behaviour is visible rather than mysterious. With no env file, or with one
#      written for a single container, none of them triggers.
#
#   2. **Starts the ticketing mock** on 127.0.0.1:8000 and waits for it to answer.
#      A single container has nowhere else to get one, and the smoke test's second
#      curl is a question about ticket data — an agent with no ticketing API behind it
#      would answer with an error and look broken.
#
#   3. **Migrates.** Skipping this produces a service that starts, answers `/ping` as
#      healthy, and fails the first real request with "no such table" — which reads
#      like a bug in a handler rather than a missing schema. The service image's own
#      entrypoint argues this at length; the reason it is repeated here is that this
#      container has its own filesystem and its own /data volume.
#
#   4. **Replaces itself with the agent**, so `docker stop` sends SIGTERM straight to
#      the server and the lifespan shutdown — which stops the MCP subprocess and
#      commits cleanly — actually runs.
# ============================================================================
set -eu

MOCK_HOST=127.0.0.1
MOCK_PORT=8000

# --- 1. the three compose-only values --------------------------------------

# `TICKETING_API_KEY=${API_KEY}` in `.env.example` is compose syntax: the compose file
# interpolates it. `docker run --env-file` does not — the value arrives literally as
# the five characters `${API_KEY}` — and the MCP server would then send that as its
# X-API-Key, so *every* tool call would come back 401 while `/ping` stayed healthy.
case "${TICKETING_API_KEY:-}" in
    *'$'*)
        echo "standalone: TICKETING_API_KEY arrived as a literal '\${...}' (--env-file does not interpolate it); using API_KEY=${API_KEY:-dev-key-123}"
        export TICKETING_API_KEY="${API_KEY:-dev-key-123}"
        ;;
    '')
        export TICKETING_API_KEY="${API_KEY:-dev-key-123}"
        ;;
esac

# `TICKETING_BASE_URL=http://ticketing:8000` names a compose service. Nothing resolves
# that name here, so the repair is to point it at the mock this entrypoint is about to
# start. Checked by resolution rather than by string comparison, so a URL pointing
# anywhere reachable is left exactly as the operator set it.
if [ -n "${TICKETING_BASE_URL:-}" ] && ! python -c "
import socket, sys, urllib.parse

host = urllib.parse.urlparse(sys.argv[1]).hostname or ''
if not host:
    sys.exit(1)
try:
    socket.getaddrinfo(host, None)
except OSError:
    sys.exit(1)
" "$TICKETING_BASE_URL"; then
    echo "standalone: TICKETING_BASE_URL=${TICKETING_BASE_URL} does not resolve in this container (it names a compose service); using http://${MOCK_HOST}:${MOCK_PORT}"
    export TICKETING_BASE_URL="http://${MOCK_HOST}:${MOCK_PORT}"
fi
export TICKETING_BASE_URL="${TICKETING_BASE_URL:-http://${MOCK_HOST}:${MOCK_PORT}}"

# `MCP_TRANSPORT=http` expects a second container on MCP_HOST:MCP_PORT. There is none
# here, so the agent is handed the stdio transport and launches the server itself —
# which is what the image ships the MCP server for. Probed rather than assumed: if
# somebody does put an MCP server on that port, it is used.
if [ "${MCP_TRANSPORT:-stdio}" = "http" ] && ! python -c "
import socket, sys

probe = socket.socket()
probe.settimeout(0.5)
try:
    probe.connect((sys.argv[1], int(sys.argv[2])))
except OSError:
    sys.exit(1)
" "${MCP_HOST:-127.0.0.1}" "${MCP_PORT:-8081}"; then
    echo "standalone: MCP_TRANSPORT=http but nothing is listening on ${MCP_HOST:-127.0.0.1}:${MCP_PORT:-8081}; using stdio"
    export MCP_TRANSPORT=stdio
    export MCP_STDIO_COMMAND="${MCP_STDIO_COMMAND:-python -m ticketing_mcp}"
fi

# --- 2. the mock, in this container ----------------------------------------

echo "standalone: starting the ticketing mock on ${MOCK_HOST}:${MOCK_PORT}"
(
    cd /app/mock-ticketing
    exec uvicorn app:app --host "$MOCK_HOST" --port "$MOCK_PORT" --log-level warning
) &

# Waited for rather than slept on: the agent's MCP session is opened at startup, and an
# unreachable mock at that moment means the container comes up with no tools at all.
attempts=0
until python -c "
import sys, urllib.request

try:
    urllib.request.urlopen('http://${MOCK_HOST}:${MOCK_PORT}/health', timeout=1).read()
except Exception:
    sys.exit(1)
"; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 40 ]; then
        echo "standalone: the mock did not answer /health within 20s; continuing — tool calls will report the failure" >&2
        break
    fi
    sleep 0.5
done

# --- 3. migrate, then 4. serve ---------------------------------------------

cd /app/services/agent
echo "standalone: applying migrations (DATABASE_URL=${DATABASE_URL:-unset})"
python -m alembic upgrade head

echo "standalone: starting the agent on ${AGENT_HOST:-0.0.0.0}:${AGENT_PORT:-8080}"
cd /app
exec python -m agent_service "$@"
