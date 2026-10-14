# Stage 15 — The documentation set, and the recorded run

Stage 15 is the one whose deliverable is prose, which makes it the stage where it is easiest to write
something plausible that is not true. So the rule for the whole stage was stricter than for code:
**nothing goes in a document that a command has not printed or a file does not contain**, and where
something could not be shown, the document says so.

Two artefacts came out of it that a reviewer will actually look at: the recorded run in
[`docs/media/`](../media/README.md), and the eight architecture documents under
[`docs/architecture/`](../architecture/overview.md).

## What was built

### The recorded run

The assignment asks for "screenshots or a short recording of one run from start to finish: a read, a
proposed write, the approval, and a failure being handled", plus the output of the eval run. Five
PNGs and one transcript are committed:

| Frame | What it shows |
|---|---|
| `01-loaded.png` | the page, loaded, with no proposal outstanding |
| `02-read.png` | a read: `search_tickets` executed, its result in the transcript |
| `03-proposed-write.png` | the approval card: the tool, its arguments, and when it expires |
| `04-approved-write.png` | the decision executed: `WRITE EXECUTED / Created ticket TCK-1025.` |
| `05-failure-handled.png` | a failing backend: the red `search_tickets` call, 4 attempts, and "That did not work, and I am not going to keep retrying it." |

They are produced by `devtasks screenshots` (`make screenshots`), which drives a real Chrome over the
DevTools Protocol against the running service. The eval transcript is
[`docs/media/eval-run.txt`](../media/eval-run.txt), and
[`docs/media/README.md`](../media/README.md) says what each frame is, how to regenerate it, and why
the transcript contains PowerShell noise rather than being tidied.

### The driver, and the task

| File | What it is |
|---|---|
| `devtasks/chrome.py` | a narrow CDP driver: launch, `goto`, `eval`, `wait_for`, `screenshot`, `close` |
| `devtasks/tasks.py` | gained the `screenshots` task and its helpers: `_capture_happy_path`, `_capture_failure`, `_count`, `_submit`, `_await_more`, `_agent_is_answering` |
| `devtasks/__main__.py`, `Makefile` | registered, so it runs as `devtasks screenshots` and `make screenshots [ARGS=--failure]` |

### The architecture set

Eight documents, each answering a question the assignment asks by name:

| Document | The question it answers |
|---|---|
| [`overview.md`](../architecture/overview.md) | how the pieces fit, and one request end to end |
| [`tool-catalog.md`](../architecture/tool-catalog.md) | the tools, their naming and descriptions, how errors reach the model, how the MCP server authenticates |
| [`approval-state-machine.md`](../architecture/approval-state-machine.md) | the write gate: states, transitions, what survives a restart, what is accepted as a failure mode |
| [`data-model.md`](../architecture/data-model.md) | what is stored, and the one invariant the schema enforces |
| [`memory.md`](../architecture/memory.md) | what memory keeps, where, for how long, for whom |
| [`observability.md`](../architecture/observability.md) | what is traced, and a worked debugging walkthrough |
| [`failure-handling.md`](../architecture/failure-handling.md) | retries, the deadline, and giving up honestly |
| [`the-mock.md`](../architecture/the-mock.md) | the fixture, its deliberate faults, and how to turn each on |

### The README, the runbooks, and the AWS design

- `README.md` was rewritten to answer the assignment's **own a-k headings**, since the earlier version
  mapped them onto engineering stages, which is not the same thing.
- `docs/runbooks/` was created with four runbooks: `local-dev.md`, `evals.md`, `provider-setup.md`
  and `troubleshoot.md`. The first three describe a workflow; the fourth is written symptom-first,
  because a symptom is what you have when you open it.
- `AGENTCORE.md` maps the contract onto AWS Bedrock AgentCore: what to keep, what to change, the
  per-service mapping, where it gets hard, and an explicit "what I would verify before relying on it"
  table.

## Decisions

### The screenshots are generated, not captured by hand

A PNG with no way to reproduce it is decoration. `devtasks/chrome.py` plus the task mean the frames
can be regenerated after a UI change, and `docs/media/README.md` documents the exact commands. The
cost is a committed browser driver; the benefit is that a reviewer can check the claims in the frames
rather than trust them.

### No Playwright, no Selenium

`websockets` 17.2 is already in `uv.lock` (a uvicorn extra), so a CDP driver is the standard library
plus one existing dependency, in about 180 lines. Adding a browser-automation framework would have
added a dependency, a browser download and a second failure mode to a task whose whole job is to take
five pictures. The driver is deliberately narrow: it does not try to be a test framework, which is why
`evals.md` proposes Playwright separately for *asserting* on the page.

### The six missing documents were written, not the links deleted

`README.md` and `docs/README.md` linked to `architecture/overview.md`, `architecture/tool-catalog.md`
and four others that did not exist. Deleting the links would have been defensible housekeeping and
would have left the repository with no answer to six of the assignment's questions. Writing them was
the honest reading of the plan of record, which had listed them since stage 0.

### The README answers the brief's headings, not the stages

The previous README was organised around the engineering stages, which reads as a progress log. The
brief asks for specific things: setup, architecture, agent design, tool design, evals, observability,
decisions, standards, AI use, improvements, authorship. A document that answers its own outline
instead of the reader's is the most common way a good project reads badly, so the headings are now
`## a.` through `## k.`, and each one answers the brief's question in the brief's own order.

### The eval transcript is committed exactly as it printed

`docs/media/eval-run.txt` contains PowerShell's `NativeCommandError` banners wrapped around uv's
ordinary stderr logging. Tidying it would have made it prettier and less true. The reason is recorded
in `docs/media/README.md` instead, so a reader who wonders is answered rather than misled.

### Typographic punctuation, sparingly

This stage wrote eight documents and a README in one pass, and the tooling that renders them is not
mine to control. Typographic punctuation is therefore used only in prose and never inside a copyable
command, so a `grep` for a command in the docs finds it verbatim.

## Defects found while building this

### 1. Six architecture documents were linked and did not exist

`README.md` and `docs/README.md` pointed at `architecture/overview.md`, `tool-catalog.md`,
`data-model.md`, `memory.md`, `observability.md` and `approval-state-machine.md`. None existed; the
directory held one unrelated file. This is the failure mode the whole stage is exposed to: the
documentation described a repository that had not been written, and it read perfectly.

Found by following the links rather than by reading the prose. That is now the check: the README's
"where each answer lives" table is a list of paths, and each one has been opened.

### 2. The README described the eval suite wrongly

The first draft said `make eval` runs "against a running stack". `eval/stack.py` starts its own mock
and agent on free ports and never touches compose. The claim was plausible, appeared helpful, and was
false. Found by opening the runner while writing the evals runbook, which is also why `evals.md` now
states explicitly what the suite does *not* use Docker for, so the next reader does not have to
rediscover it.

### 3. An eval family was mislabelled against the committed report

The README grouped `timeout_gives_up` under "a run with failures switched on". The committed
`eval/results/latest.md` reports its family as "a run that runs out of time". A one-line drift between
a table someone wrote from memory and a report a machine wrote, where the report is the artefact a
reviewer will diff against. Fixed to match the report.

### 4. `---` after a paragraph is a heading, and it happened six times

In Markdown, a line of three hyphens directly under a paragraph is a setext heading, not a rule. Both
`stage-14.md` (twice) and the new README (four times) had `---` with no blank line before it, which
would have rendered whole sections at heading size. Found by checking the structure rather than
trusting the source. Every `---` in this stage's output now has a blank line above it.

### 5. Two lint findings in the screenshot task

`ruff` reported `SIM108` (an if/else that should be a ternary) and `RUF002` (an ambiguous en dash in a
docstring) in `devtasks/tasks.py`. Both were introduced by this stage's code and both were caught by
the gate rather than by reading, which is the intended division of labour. The second is the same class
of problem as defect 4: typographic punctuation in a place where the toolchain is entitled to be
strict.

### 6. The failure frame needs a different stack, and the shortcut would have produced a lie

`05-failure-handled.png` needs a backend that is genuinely failing, but `docker-compose.ci.yml` pins
`FLAKY=0`, so the CI stack can never produce it. With the dev stack at `FLAKY_RATE=1.0` the frame is
real. The tempting shortcut was to have the task flip the fixture's chaos flag itself and photograph
the result; that would have been a screenshot of the harness, not of the product. The task now refuses
to draw a failure the stack did not produce, and the two modes are documented as needing two stacks.

Verifying the failure was real rather than assumed:

```
$ docker compose exec -T ticketing printenv FLAKY FLAKY_RATE
1
1.0
```

### 7. A write to a new root-level file silently produced nothing

Twice, `write_to_file` to a path that did not exist yet produced no content, once an empty file and
once nothing at all, while reporting nothing useful. `append_to_file` creates and appends correctly,
so `AGENTCORE.md` was written as five appends. Recorded because the workaround is not obvious and the
next person to add a root-level document will meet the same thing.

### 8. A relative path cannot be inspected, so the frames were served over HTTP

`view_image` rejects relative paths, so the captures could not be looked at from the repository. A
throwaway `python -m http.server 8099 --directory docs/media` made them inspectable at
`http://127.0.0.1:8099/...`. Frames 02 through 05 were then checked visually (the approval card with
its arguments and expiry, the created ticket id, and the four-attempt failure with its closing
sentence) rather than trusted from a selector that matched.

## Verification

Every claim in this stage's documents comes from one of the following. The commands below are the gates
as they were run on the committed tree.

### The gates

```
$ uv run python -m devtasks lint
$ D:\agentcore-platform\.venv\Scripts\ruff.exe check
All checks passed!

$ uv run ruff format --check
146 files already formatted

$ uv run python -m devtasks typecheck
Success: no issues found in 101 source files

$ uv run python -m devtasks test-unit
====================== 376 passed, 4 deselected in 8.16s ======================

$ uv run python -m devtasks secrets
$ D:\agentcore-platform\.venv\Scripts\detect-secrets.exe scan --all-files --exclude-files (^|[\\/])(\.venv|\.git|data|__pycache__|\.mypy_cache|\.ruff_cache|\.pytest_cache|htmlcov|node_modules)[\\/]|(^|[\\/])\.secrets\.baseline$
secrets: nothing new - 3 reviewed value(s) in .secrets.baseline, 8 finding(s), all recognised
```

### The eval suite

```
$ uv run python -m devtasks eval
All 19 case(s) passed in 87.7s.
```

The full table is committed at [`eval/results/latest.md`](../../eval/results/latest.md), and the
console transcript at [`docs/media/eval-run.txt`](../media/eval-run.txt). Both were written by the
runner, not by hand.

### The recorded run

```
$ uv run python -m devtasks screenshots
wrote docs/media/01-loaded.png
wrote docs/media/02-read.png
wrote docs/media/03-proposed-write.png
wrote docs/media/04-approved-write.png

$ uv run python -m devtasks screenshots --failure
wrote docs/media/05-failure-handled.png
```

### The documentation's own check

Three properties were verified mechanically rather than by reading, because they are the three ways
this stage could be wrong:

1. **Every path the README cites exists.** The "where each answer lives" table and the repository
   layout were checked against the tree.
2. **Every number in the README was copied from output.** 19/19 in 87.7s, 376 tests, 101 type-checked
   files, 146 formatted files, three reviewed secret values.
3. **Every architectural claim names a file that contains it.** Each document points at the module or
   table it describes, so a reviewer can disagree with the prose by opening the code.

### What was not verified

- **The rendered appearance of the documents.** They were checked as source for heading structure
  (defect 4) but not rendered in a Markdown viewer to confirm every table lays out.
- **`AGENTCORE.md`'s AWS specifics.** It was written from AWS's public documentation rather than from a
  deployment; the verification table inside it is the list of things that would have to be checked on a
  real account, and it says so.

## Definition of done

| DoD item | Result |
|---|---|
| Screenshots or a recording of one run: a read, a proposed write, the approval, and a failure being handled | **Met.** Five frames, generated by a committed task from a real run, with the failure frame produced against a genuinely failing backend. |
| The output of the eval run | **Met.** `eval/results/latest.md` (19/19 in 87.7s) and `docs/media/eval-run.txt`, both written by the runner. |
| The architecture is documented where the README says it is | **Met.** Six documents that were linked and missing now exist; all eight are listed in one table and each was opened. |
| The README answers the assignment | **Met.** Headings `a.` through `k.`, matching the brief's own outline. |
| A runbook for running it, and for when it breaks | **Met.** Four runbooks, including a symptom-first troubleshoot page built from the failures this project actually hit. |
| The AWS design, with what to verify | **Met.** `AGENTCORE.md`, working from the AWS documentation, ending in an explicit verification list. |
| Nothing claimed that a command has not printed | **Met, and it is the rule that found defects 1 to 3.** Every number in the stage's output is quoted from a run; the three defects were all a document disagreeing with the code. |
| The gates stay green | **Met.** Lint, format, mypy (101 files), 376 unit tests and the secret scan all pass on the committed tree. |

### What a reviewer should check first

1. `docs/media/` — open `03-proposed-write.png` and `05-failure-handled.png`. Those two frames are the
   assignment's approval requirement and its failure requirement, in the product rather than in prose.
2. `make screenshots` on a running stack, and confirm it writes the same four frames. A capture nobody
   can reproduce is the one thing this stage's decisions were designed to avoid.
3. The README's "where each answer lives" table, opening one row at random: the table is the claim, and
   the paths are the evidence.
4. `docs/architecture/approval-state-machine.md` against `agent/gate.py` and
   `db/repositories/approvals.py`. The document asserts that the refusal is enforced below the route;
   that is checkable in about two minutes and is the load-bearing claim of the design.
