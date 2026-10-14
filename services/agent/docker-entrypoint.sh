#!/bin/sh
# ============================================================================
# Container entrypoint for the agent service: migrate, then serve.
#
# The order is not decoration. Skipping the migration produces a service that
# starts successfully, answers /ping as healthy, and fails the first user request
# with "no such table" — a failure that reads like a bug in a handler rather than
# a missing schema, and that no healthcheck can catch because the healthcheck
# never touches the database.
#
# Migrating on every start rather than in a separate release step is a deliberate
# trade for this deployment. It is safe because the migrations are idempotent
# (`upgrade head` on an up-to-date database is a no-op) and because exactly one
# replica runs; it would need revisiting for a multi-replica rollout, where two
# instances racing the same migration is a real hazard.
#
# `exec` is what makes this a good citizen: the shell is replaced by the server,
# so `docker stop` sends SIGTERM straight to uvicorn and the lifespan shutdown —
# which is what stops the MCP subprocess and commits cleanly — actually runs.
# Without it, PID 1 is the shell, the signal never reaches Python, and the
# container is SIGKILLed ten seconds later.
# ============================================================================
set -eu

# Alembic's `script_location` is relative, so it has to run from the service
# directory — the same reason `devtasks migrate` pins a working directory.
cd /app/services/agent

echo "agent: applying migrations (DATABASE_URL=${DATABASE_URL:-unset})"
python -m alembic upgrade head

echo "agent: migrations applied; starting the service"
exec python -m agent_service "$@"
