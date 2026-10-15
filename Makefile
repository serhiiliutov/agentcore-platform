# ============================================================================
# AgentCore Platform — task runner
#
# This Makefile is a thin, conventional wrapper. Every target is a one-line
# delegation to `python -m devtasks`, which is where the actual logic lives, so
# the same commands work on Windows (where GNU Make is often not installed):
#
#     uv run python -m devtasks help
#
# `make` is provided because reviewers, macOS/Linux users and CI all reach for
# it, and CI runs the targets in this file so they cannot silently rot.
#
#   make help          list every command with an example
#   make bootstrap     sync the environment and create .env
#   make up            start the whole stack
# ============================================================================

.DEFAULT_GOAL := help
SHELL := /bin/sh

UV  ?= uv
RUN := $(UV) run python -m devtasks

.PHONY: help bootstrap doctor json \
        lint fmt typecheck format \
        test test-unit test-integration \
        up down ps logs up-ci down-ci ps-ci reset mock mcp chat \
        migrate downgrade revision \
        eval secrets screenshots env clean ci

# --- forward trailing arguments: `make test-unit ARGS="-k approval -x"` ------
ARGS ?=

## help: list every command with an example
help:
	@$(RUN) help

## bootstrap: create the virtual environment, lock file and .env
bootstrap:
	$(UV) sync
	$(RUN) env

## doctor: check tools, credentials and whether the stack is answering
doctor:
	@$(RUN) doctor

## json: print the repository layout as JSON
json:
	@$(RUN) json

# --- quality gates ----------------------------------------------------------
## lint: ruff check
lint:
	@$(RUN) lint $(ARGS)

## fmt: ruff format plus safe autofixes (mutates files)
fmt:
	@$(RUN) fmt $(ARGS)

## format: alias for fmt
format: fmt

## typecheck: mypy over services, tests and dev tooling
typecheck:
	@$(RUN) typecheck $(ARGS)

## test: the whole suite with coverage
test:
	@$(RUN) test $(ARGS)

## test-unit: tests needing no stack and no model credential
test-unit:
	@$(RUN) test-unit $(ARGS)

## test-integration: tests needing the stack up
test-integration:
	@$(RUN) test-integration $(ARGS)

## ci: lint, typecheck, unit tests and evals — what CI runs
ci:
	@$(RUN) ci $(ARGS)

## secrets: scan the working tree for credentials
secrets:
	@$(RUN) secrets $(ARGS)

# --- runtime ----------------------------------------------------------------
## up: build and start the whole stack in the background
up:
	@$(RUN) up $(ARGS)

## down: stop the stack (keeps the data volume)
down:
	@$(RUN) down $(ARGS)

## ps: service status including healthchecks
ps:
	@$(RUN) ps $(ARGS)

## logs: tail logs; narrow with `make logs ARGS=agent`
logs:
	@$(RUN) logs $(ARGS)

## up-ci: start the CI stack — same services, deterministic, waits for health
up-ci:
	@$(RUN) up-ci $(ARGS)

## down-ci: stop the CI stack; `make down-ci ARGS=-v` drops its volumes too
down-ci:
	@$(RUN) down-ci $(ARGS)

## ps-ci: CI stack status, including healthchecks
ps-ci:
	@$(RUN) ps-ci $(ARGS)

## reset: restore the ticketing seed data
reset:
	@$(RUN) reset $(ARGS)

## mock: run the mock ticketing API locally, blocking (honours FLAKY=1)
mock:
	@$(RUN) mock $(ARGS)

## mcp: run the MCP server locally over stdio, blocking
mcp:
	@$(RUN) mcp $(ARGS)

# --- database ---------------------------------------------------------------
## chat: run the Chainlit chat front end against a running agent, blocking
chat:
	@$(RUN) chat $(ARGS)

## migrate: apply all Alembic migrations
migrate:
	@$(RUN) migrate $(ARGS)

## downgrade: roll back (default: base)
downgrade:
	@$(RUN) downgrade $(ARGS)

## revision: autogenerate a migration — make revision ARGS="add approvals"
revision:
	@$(RUN) revision $(ARGS)

# --- evals and housekeeping -------------------------------------------------
## eval: run the eval suite and print a pass/fail table
eval:
	@$(RUN) eval $(ARGS)

## screenshots: capture one run into docs/media (needs the stack up)
##              add ARGS=--failure once the mock is failing on purpose
screenshots:
	@$(RUN) screenshots $(ARGS)

## env: create .env from .env.example if it does not exist
env:
	@$(RUN) env

## clean: remove caches and local data; add DEEP=1 to drop .venv too
clean:
	@$(RUN) clean $(if $(DEEP),--deep,)
</content>
