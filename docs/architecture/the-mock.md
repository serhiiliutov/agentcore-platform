# The ticketing mock, and how it misbehaves

`mock-ticketing/` is the starter kit's fixture, kept **unmodified**: surviving its failures is the
exercise, so editing the thing that fails would be editing away the task.
[`tests/unit/test_starter_kit_integrity.py`](../../tests/unit/test_starter_kit_integrity.py)
recomputes the checksums in [`docs/starter-kit/checksums.sha256`](../starter-kit/checksums.sha256) and
fails if the fixture changed by a single byte.

This document is what it does, what it does badly on purpose, and how to drive each failure.

## The surface

FastAPI, in-memory state seeded from `seed.py`, resettable. Endpoints:

| Endpoint | Notes |
|---|---|
| `GET /health` | unauthenticated; reports the ticket count and whether flakiness is on |
| `GET /tickets` | search, filters + pagination (`page`, `page_size` ≤ 50) |
| `GET /tickets/{id}` | one ticket, with comments |
| `POST /tickets` | create; `title` 3–120 chars, `requester` required, `priority` from a closed set |
| `PATCH /tickets/{id}` | update status, priority or assignee |
| `POST /tickets/{id}/comments` | add a comment |
| `POST /admin/reset` | restore the seed |
| `GET /docs`, `/openapi.json` | FastAPI's own |

`/tickets` and `/admin` require `X-API-Key`, checked in **one middleware** before anything else, so
an unauthenticated call is a 401 regardless of route. The default is the kit's documented
`dev-key-123`; `.env` supplies the value, and only `mcp-ticketing` holds it.

The seed is a handful of tickets with realistic IT subjects — printers, VPNs, laptops — which is what
makes the agent's behaviour demonstrable: there is something to find, and something plausible to add.

## The three ways it fails on purpose

Configured entirely by environment, so nothing about the failure mode is in code:

| Variable | Effect |
|---|---|
| `FLAKY=1` | a share of authenticated requests fail |
| `FLAKY_RATE` | that share (default `0.3`) |
| `FLAKY_SEED` | fixes the RNG, making a flaky run reproducible |
| `LATENCY_MS` | a delay added to every authenticated request |

With `FLAKY=1`, a failing request is either a **429 with `Retry-After: 1`** or a **500**, chosen at
random per request. Both are real-world failure modes with different correct responses, and having
both is what makes the retry policy meaningful rather than a single if-branch: a rate limit is worth
waiting out, and a server error may be worth retrying immediately.

`FLAKY_SEED` is the variable that makes this testable. Without it, "the failures case passes" could
be luck; with it, the same sequence of failures happens every run, which is what lets
`tests/integration/test_failure_paths.py` and the `failures_switch_on` eval case assert on a run that
actually failed.

`LATENCY_MS` is the other half of the exercise: it produces slow responses rather than errors, which
is how the deadline and the turn budget get exercised. A retry policy that only handles errors will
hang forever against a backend that is merely slow.

## How to turn each on

```bash
# ~30% of calls fail, reproducibly
FLAKY=1 FLAKY_RATE=0.3 FLAKY_SEED=7 make mock
# or, in the stack:
docker compose up -d ticketing        # with FLAKY=1 in the environment

# every response takes 3 seconds
LATENCY_MS=3000 docker compose up ticketing

# everything fails, for the failure screenshots
FLAKY=1 FLAKY_RATE=1.0 docker compose up ticketing
```

The `docker-compose.ci.yml` override pins `FLAKY=0` and `LATENCY_MS=0`, deliberately: a pipeline that
fails two runs in five is a pipeline people learn to re-run without reading. See the reasoning in
[`plan/stage-14.md`](../plan/stage-14.md).

## What the agent does about each

| The mock does | The MCP server does | The agent reports |
|---|---|---|
| 401 | `Authentication` — not retryable | "configuration problem, not a retry" |
| 404 | `NotFound` | "check the id before retrying" |
| 400 (validation) | `Validation` | "fix the arguments" |
| 429 | retries with backoff, honouring `Retry-After` | "waiting is required before retrying" |
| 500 | retries with backoff | after the budget: "attempted N times, given up" |
| accepts a write, response lost | **does not retry** — `Unresolved` | "check the ticket list before deciding whether to try again" |
| slow (`LATENCY_MS`) | the turn deadline expires | "I ran out of time", with the partial transcript |

The last two rows are the interesting ones and both are the subject of eval cases:
`failures_do_not_duplicate_a_write` asserts the ticket count after a lost response, and
`timeout_gives_up` asserts that a slow backend produces a truthful failure rather than a hang. The
taxonomy itself is in [`tool-catalog.md`](tool-catalog.md); the retry policy and the deadline are in
[`failure-handling.md`](failure-handling.md).

## What the mock deliberately cannot do

- **No users, no permissions.** Every authenticated caller sees every ticket. It is a fixture for
  tool-calling, not an authorization system, which is why no tool takes a `user_id`.
- **No persistence.** State is a dictionary; a restart re-seeds. That is why `POST /admin/reset`
  exists, and why each eval run can start from a known fixture.
- **No search ranking.** `q` is a case-insensitive substring match on title and description, sorted
  by `updated_at`. It is deliberately naive: the agent's job is to cope with a plain backend, not a
  clever one.
- **No idempotency of its own.** The mock will happily create two tickets for two identical requests
  — which is precisely what makes the guarantee interesting, because it has to be provided *above* the
  fixture rather than by it.
