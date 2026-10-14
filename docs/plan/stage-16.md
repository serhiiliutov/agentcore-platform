# Stage 16 — Clean-slate verification, and submission

The plan of record for this stage is one sentence long and unforgiving: *clone into a temp directory,
`make up`, `make eval`, browser walkthrough — to prove nothing depends on my machine.* Everything
below is that, plus the history scan the DoD asks for, plus the two things the plan predicted would
be awkward (no tags yet, and `gh` authenticated as the wrong account).

**Result: every result reproduced from a fresh clone of the committed tree. The one step this stage
could not finish is the push, which is blocked for a reason nobody predicted — the repository the
remote names does not exist. It is written up under [Pending](#pending-the-push), with the evidence
and the three ways to finish it.**

## What was verified

### A clone, not this working tree

```bash
$ git clone --quiet d:/agentcore-platform "$env:TEMP\agentcore-verify"
$ git -C "$env:TEMP\agentcore-verify" log --oneline -1
3425f41 (HEAD -> main, origin/main, origin/HEAD) docs(stage-15): the documentation set, and a run you can regenerate

$ (Test-Path .venv), (Test-Path data), (Test-Path .env)
False False False
```

No virtual environment, no `data/`, no `.env`: exactly what a reviewer gets. The clone is of the
*committed* tree, so nothing untracked — no local database, no trace file, no scratch script — can be
part of what passed.

### The dev loop, in the clone

```bash
$ uv --directory $dest sync --all-groups
 + uvicorn==0.54.0
 + watchfiles==1.3.0
 + websockets==17.2

$ uv --directory $dest run python -m devtasks test-unit
===================== 376 passed, 4 deselected in 20.11s ======================
```

### The eval suite, in the clone

```bash
$ uv --directory $dest run python -m devtasks eval
All 19 case(s) passed in 63.6s.
```

The committed run took 87.7s and this one 63.6s. The difference is entirely timing, and that is
checkable rather than asserted:

```bash
$ git -C $dest diff --stat eval/results/
 eval/results/latest.json | 118 ++++++++++++++++++++-----------------
 eval/results/latest.md   |   2 +-
 2 files changed, 60 insertions(+), 60 deletions(-)

$ git -C $dest diff eval/results/latest.md
-**All 19 case(s) passed** in 87.7s.
+**All 19 case(s) passed** in 63.6s.
```

One changed line in the report, and it is the duration. Every case's verdict, family and check count
is identical, and `latest.json` differs only in the per-case timings the runner records. The suite is
reproducible in its *judgements*, and not in its *stopwatch* — which is the honest form of the claim.

### The stack, in the clone, with the documented command

```bash
$ cp .env.example .env
$ docker compose -f "$dest/docker-compose.yml" --project-directory $dest up -d --build --wait
   EXIT=0

$ docker compose -f "$dest/docker-compose.yml" --project-directory $dest ps
SERVICE         STATUS
agent           Up 30 seconds (healthy)
mcp-ticketing   Up 41 seconds (healthy)
ticketing       Up 47 seconds (healthy)

$ curl -s localhost:8080/ping
{"status":"Healthy"}
```

Images built from the clone's own Dockerfiles, three services healthy, one exit code. This is also
fresh evidence for the build: the images had only ever been built from the working tree before.

### The browser walkthrough, in the clone

```bash
$ uv --directory $dest run python -m devtasks screenshots
wrote docs/media/01-loaded.png
wrote docs/media/02-read.png
wrote docs/media/03-proposed-write.png
wrote docs/media/04-approved-write.png

$ docker compose ... exec -T ticketing printenv FLAKY FLAKY_RATE     # chaos genuinely on
1
1.0
$ uv --directory $dest run python -m devtasks screenshots --failure
wrote docs/media/05-failure-handled.png
```

The capture task clicks the real approval button in a real browser against the clone's stack, and the
result is in the clone's database rather than in a screenshot:

```
$ docker compose ... exec -T agent python -c "..."
approvals by action/state: [('create_ticket', 'executed', 1)]
messages: 8
traces: 4
latest trace: ('ok', 1, 11)
```

One `create_ticket` approval, in state `executed`: the read, the proposal and the approval all
happened, in the clone, through HTTP.

**The frames are not byte-identical to the committed ones, and that is the interesting part.** One is:

| Frame | committed | clone | |
|---|---|---|---|
| `01-loaded` | `7DDB5240B726` | `7DDB5240B726` | identical |
| `02-read` | `A7FB78F99AED` | `02DC71C3DD34` | differs |
| `03-proposed-write` | `CE393EE02700` | `9001A8E7C986` | differs |
| `04-approved-write` | `E66448EDCBE5` | `9BD38A0B8C85` | differs |
| `05-failure-handled` | `E7E5777F94C4` | `1AD037D73CFE` | differs |

The first frame is identical because it renders a page with no run in it. The other four differ
because the page shows values a live run cannot fix: the session id in the header
(`sess-eab3f88efee8…`) and the approval's UUID, both generated per run. Checking rather than guessing
is what turned "the screenshots differ" into a statement with a cause: serving the clone's frames over
a throwaway HTTP server and looking at them showed `04` reading `WRITE EXECUTED / Created ticket
TCK-1025.` — the same ticket id, the same three turns, the same card — and `05` reading the same red
`search_tickets` call, the same *"attempted 4 times and has been given up on rather than retried
further"*, and the same closing sentence.

That is why the committed frames are paired with a task that regenerates them. A PNG is not a
reproducible artefact; a PNG *and the command that produced it* is.

### No secret in history

The DoD names `gitleaks` over the full history. It is not installed here, so the substitution is
stated rather than quietly skipped:

```bash
$ Get-Command gitleaks
no - falling back to detect-secrets plus a pattern scan over the full history

$ (git rev-list --all | Measure-Object).Count
85

$ git log -p --all | Select-String <patterns>
Count Name
----- ----
   27 dev-key-123
```

Eighty-five commits scanned for the shapes that matter (`sk-ant-…`, `sk-…`, `ghp_…`, `github_pat_…`,
`AKIA…`, `xox…`, `BEGIN … PRIVATE KEY`): **no match**. The only secret-shaped string in the entire
history is `dev-key-123`, the starter kit's documented default key, twenty-seven times — it is public
by design and is one of the three reviewed values in `.secrets.baseline`.

`detect-secrets` covers the working tree in the same form CI runs it (see
[`stage-14.md`](stage-14.md) for why it, rather than the `gitleaks` Action, and for the three ways
that gate previously could not fail). The gap that remains is real and small: a pattern scan is not
an entropy scan, so a high-entropy secret in a shape none of those patterns match would not be
caught by this line. The history is 85 commits of a repository whose only credential is a fixture's
default, so the residual risk is a reviewer's judgement call rather than a blind spot.

## Decisions

### The clone is of the working tree, not of GitHub

`git clone d:/agentcore-platform` proves the committed tree is self-sufficient, which is the claim.
Cloning from `origin` would additionally prove the push succeeded, and that is verified by the push
itself rather than by a second clone.

### The frames were served over HTTP and looked at

`view_image` accepts a URL, not a path, and rejects `file://` ("protocol mismatch"), so a throwaway
`python -m http.server` on port 8098 made the clone's captures inspectable. Without that step the
honest report would have been "the hashes differ and I do not know why", which is exactly the kind of
unexamined statement this project has spent fifteen stages refusing to make.

### What was *not* re-run

- **The `linux/arm64` cross-build.** It was verified in [stage 14](stage-14.md) and nothing that
  feeds it has changed since: the Dockerfiles copy `uv.lock`, the per-service `pyproject.toml`s,
  `services/*/src/**`, each service's README and the alembic migrations — none of which stage 15
  touched, since stage 15 changed documentation, `devtasks/` and `README.md` only. The new evidence
  this stage adds is that the images build from a clone's own context.
- **A second machine and a second Docker version.** Still not done, and still the honest limitation
  named in the README's section j: this is one Windows host with Docker 26.1.1, and CI is the only
  Linux execution evidence there is.
- **`gitleaks`**, as above.

## What this stage found

### 1. `git status` in a non-interactive shell opens a pager and hangs

The first verification command ended in `git diff --shortstat HEAD`, and the shell sat at a `:` prompt
with no output — git had launched a pager and was waiting for a keypress nobody was going to send.
Everything after the pipe never ran. Fixed by using `git --no-pager` everywhere, and worth recording
because the failure looks like "the repository is huge and slow" rather than "a process is waiting for
input".

### 2. The frames differ, and the reason had to be found rather than asserted

Recorded above as the most useful finding of the stage: the difference is per-run identifiers, and one
frame proves it by being byte-identical. Had the hashes not been compared, the stage file would have
claimed "the capture reproduces" without qualifying what that means.

### 3. `uv --directory` warns when a venv is active from elsewhere

```
warning: `VIRTUAL_ENV=D:\agentcore-platform\.venv` does not match the project environment path
`.venv` and will be ignored; use `--active` to target the active environment instead
```

Harmless — uv used the clone's environment, which is why the clone's tests and evals are meaningful
evidence — but a reader would reasonably wonder whether the run proved anything about the clone. It
printed a warning and then ran the clone's code; the ticket in the clone's database is the proof.

### 4. Docker Desktop was not running after a restart

Expected, already documented in the runbook, and it still cost a step: the plan's verification cannot
begin until the daemon answers, and the first `docker info` failed with the same
`Get "http://%2F%2F.%2Fpipe%2Fdocker_engine/…": The system cannot find the file specified` a reviewer
will see.

### 5. `gh` is authenticated as the wrong account

The plan predicted it: `gh auth status` reports `AIDataMaster` as the active account, while the target
is `serhiiliutov/agentcore-platform`. Pushing needs the switch, and the push is the last step of the
stage.

## Definition of done

| DoD item | Result |
|---|---|
| A fresh clone reproduces every result | **Met.** Cloned to a temp directory with no `.venv`, `data/` or `.env`: 376 unit tests pass, 19/19 eval cases pass, `docker compose up -d --build --wait` brings three services to healthy with exit 0, `/ping` answers `Healthy`, and the capture task produces the read, the proposal, the approval and the failure frame against the clone's own stack. |
| The clone run is recorded, start to finish | **Met.** The commands and their output are quoted above, including the check of the clone's own approvals table. |
| The public repo is complete | **Blocked on the push.** The committed tree is complete and self-sufficient — a clone of it passes everything — but it is not yet on GitHub. |
| No secret in history | **Met, with the substitution stated.** `gitleaks` is not installed; the full history (85 commits) was scanned with the patterns that matter and `detect-secrets` covers the tree. The only match is the fixture's documented default key. |
| Tag `v1.0.0` | **Met locally.** Created and annotated; it cannot reach GitHub until the push can. |
| Push to `serhiiliutov/agentcore-platform` | **Not met.** `remote: Repository not found`. The diagnosis and the three ways to finish are below. |

## Pending: the push

The last step of the plan is `git push`, and it fails:

```bash
$ git push origin main
remote: Repository not found.
fatal: repository 'https://github.com/serhiiliutov/agentcore-platform.git/' not found
```

Three facts explain it, and none of them is a mistake in this repository:

```bash
$ gh api user --jq .login
AIDataMaster

$ gh repo view serhiiliutov/agentcore-platform
GraphQL: Could not resolve to a Repository with the name 'serhiiliutov/agentcore-platform'.

$ gh repo list serhiiliutov --limit 15
serhiiliutov/exchange-rates         public  2026-09-06
serhiiliutov/ScamProjects           public  2023-05-18
serhiiliutov/NFTCollectionTest      public  2022-04-30
```

1. **The target repository does not exist.** `serhiiliutov` is a real account with three public
   repositories, and `agentcore-platform` is not one of them. GitHub reports a non-existent repository
   and one the token cannot see identically, so this was checked against the account's own listing
   rather than inferred from the 404.
2. **The authenticated account is not the owner.** `gh auth status` has exactly one account,
   `AIDataMaster`, with `repo` scope. There is no `serhiiliutov` account to switch to, which is the
   step the plan anticipated ("switch the active `gh` account — currently `AIDataMaster`") — it
   anticipated a switch between two authenticated accounts, and only one exists.
3. **Pushes from this clone have worked before, to something else.**

   ```bash
   $ git reflog show origin/main
   f1befdd refs/remotes/origin/main@{0}: update by push
   575cb66 refs/remotes/origin/main@{1}: update by push
   ```

   Six "update by push" entries, so the remote tracked a repository that accepted these commits. The
   URL was changed to `serhiiliutov/agentcore-platform` at some point after that, and no push has
   succeeded against the new one.

**What would finish it**, in the order I would try them:

1. **Log in as the owner and let the push create nothing** — `gh auth login` for `serhiiliutov`, then
   `gh repo create serhiiliutov/agentcore-platform --public --source . --push`. This is the plan's
   intent: published under the account the README links to.
2. **Create the empty repository** at `serhiiliutov/agentcore-platform` and add `AIDataMaster` as a
   collaborator with write access; then `git push origin main && git push origin v1.0.0` works with
   the credentials already configured.
3. **Publish under `AIDataMaster`** instead, which needs no new credentials at all — but then one line
   of the README (the clone URL) and the tag's context are wrong, so the README would have to change
   too. Worth stating because it is the cheapest option and the only one that quietly makes a document
   wrong.

Nothing else in this stage is waiting on it: the verification, the history scan, the record, the
commit and the annotated tag are all complete, and the tag is on the commit that will be pushed.

### What a reviewer should check first

1. `git log --oneline | head` — the stage commits are the record, and each one's message states what
   changed and what it cost.
2. `docker compose up -d --wait` from a clone, then `http://localhost:8080/`. That is the whole
   bring-up, and it is the command the README's first section promises.
3. `make eval` — 19 cases, and the report it writes is diffable against the committed one, which is
   how the "same verdicts, different stopwatch" claim above is checkable rather than trusted.
4. `git log -p --all | grep -E 'sk-|ghp_|AKIA|PRIVATE KEY'` — the history scan, by hand.
