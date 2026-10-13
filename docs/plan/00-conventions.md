# Working conventions

These are the rules the repository is built to. They exist so that a reviewer —
or a teammate joining later — can predict what they will find without asking.

---

## 1. What is committed, and what is never committed

**Committed**

* All source, tests, migrations, Dockerfiles and compose files.
* `uv.lock`. A lock file is the reproducibility guarantee; without it
  "it works on my machine" is the only thing we can honestly say.
* `docs/` — the plan, the ADRs, the architecture notes and the runbooks.
* `docs/agent-platform-assignment/` — the task as given, unedited.
* `eval/cases.json` and `eval/results/latest.{md,json}` — the eval evidence a
  reviewer is asked to look at.
* `docs/media/*.png` and small `.gif` recordings.

**Never committed** (enforced by `.gitignore`, `detect-secrets` in CI and
`tests/unit/test_secret_hygiene.py`)

* `.env` and any `.env.*` other than `.env.example`.
* API keys, tokens, PEM files, credentials of any kind.
* `data/` — the SQLite database, its WAL files and the JSONL traces.
* Caches, virtual environments, `node_modules`.
* `docs/internal/`, `docs/scratch/`, `notes/`, `*.local.md` — private working
  notes. The repository is a submission, not a diary.
* Raw video recordings. Screenshots and small GIFs only.

If a secret ever does reach a commit, the fix is to rotate the credential first
and rewrite history second. Rotation is the part that actually matters.

---

## 2. Commits

Conventional Commits, one logical change per commit, imperative mood:

```
feat(mcp): add pagination hint to search_tickets
fix(agent): expire pending approvals after the configured TTL
test(approval): prove a declined write touches the mock zero times
docs(adr): record why Alembic is used in one service and not the other
chore(deps): lock the model SDKs
```

The body explains **why**, not what — the diff already says what. Stages land as
one or more commits, and a commit should leave the tree in a state where
`make lint` and `make typecheck` pass.

Nothing is committed that is not verified. If a stage's definition of done is not
met, the work stays uncommitted, or lands behind a clearly-labelled incomplete
commit that the stage log records.

---

## 3. Tests

* `@pytest.mark.integration` for anything needing the stack running.
* `@pytest.mark.live` for anything needing a real model credential. Deselected by
  default so CI never depends on a paid key.
* No test may sleep beyond a couple of seconds, and no test may depend on network
  access to anything outside localhost.
* Test names read as sentences:
  `test_a_declined_write_reaches_the_mock_zero_times`.
* Failures must be diagnosable from the failure message alone.

---

## 4. Code

* One file per concern. Files past roughly 150 lines get split; functions past
  roughly 40 lines get a helper extracted.
* Full type annotations, `mypy --strict`. `Any` requires a comment justifying it.
* No hand-rolled HTML escaping, no bare `except:`, no retry loop without a
  budget, no `print` outside `devtasks/` and the CLI entry points.
* Errors a model will read are written for a model: what happened, whether
  retrying can help, and what to do instead.
* Comments explain intent and trade-offs. A comment that restates the code is
  deleted in review.

---

## 5. Secrets and credentials

* Credentials come from the environment, never from a file in the tree.
* A credential must never appear in a log line, a trace span, a database row, an
  HTTP response, or an error message. Asserted by a test, not by intent.
* The runtime key entry path (`ALLOW_RUNTIME_KEY_ENTRY=1`) is off by default, is
  documented as development-only, keeps keys in process memory, and never
  persists them. Its endpoint is deliberately outside the AgentCore contract
  surface.
* `detect-secrets` runs locally; the gitleaks Action runs in CI as an independent
  second opinion.

---

## 6. Stages and review

* `docs/plan/MASTER-PLAN.md` is the plan of record. `docs/plan/stage-NN.md` is the
  working log for one stage: goal, files, definition of done, and the result that
  was actually observed.
* A stage is done when its **Verify** command passes on a clean checkout — not
  when the code looks finished.
* Anything deliberately left out is written down in the README under "what the
  evals do not catch" or "what I would do differently", never silently dropped.

---

## 7. Estimates

Time is reported in two columns because they measure different things:

* **Agent working time** — how long this work takes when it is produced and
  verified with an AI pair, including the verification runs.
* **Human engineering days** — what the same work costs a senior engineer with
  normal tooling, including review and the documentation a team would expect.

They are not convertible. A human day is not eight agent hours, and the agent
column does not include the time a person spends deciding what to build.
