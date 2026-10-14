# Documentation

Start here. Everything in this directory is written to be read by a reviewer who
has the repository open in front of them.

## Contents

| Path | What it is |
|---|---|
| [`agent-platform-assignment/`](agent-platform-assignment/) | The task as it was given to us. Committed unedited so the work can be judged against it. |
| [`starter-kit/`](starter-kit/) | The unedited starter-kit README and sample eval cases, kept for provenance. The kit's code now lives at the repository root: `mock-ticketing/`, `CONTRACT.md`, `docker-compose.yml`. |
| [`plan/MASTER-PLAN.md`](plan/MASTER-PLAN.md) | **The plan.** Scope, locked decisions, 17 stages, per-stage estimates, risks, definition of done. |
| [`plan/00-conventions.md`](plan/00-conventions.md) | Working agreements: what is committed and what is not, commit style, branch/review flow. |
| `plan/stage-NN.md` | One file per stage: what was built, the decisions taken, the defects the stage found, the verification output, and the definition of done. |
| `adr/` | Architecture decision records. Each records the choice, the reasoning, and what was rejected. |
| `architecture/` | How the system works: overview and diagram, data model, tool catalogue, approval state machine, memory, observability, failure handling, and what the ticketing mock actually does. |
| `runbooks/` | Operational how-to: local development, running the evals, configuring model providers, troubleshooting. |
| `media/` | Screenshots and the recorded end-to-end run. |
| `internal/` | **Not committed.** Private working notes. Gitignored on purpose. |
| `scratch/` | **Not committed.** Throwaway analysis. Gitignored on purpose. |

## Reading order for a reviewer

1. [`../README.md`](../README.md) — the assignment answers, section by section.
2. [`../AGENTCORE.md`](../AGENTCORE.md) — how this would run on AWS with Amazon Bedrock AgentCore.
3. [`plan/MASTER-PLAN.md`](plan/MASTER-PLAN.md) — what we set out to build and in what order.
4. [`architecture/overview.md`](architecture/overview.md) — how the pieces fit together.
5. [`architecture/tool-catalog.md`](architecture/tool-catalog.md) — the tools the model actually sees, with their schemas and error contracts.
6. [`architecture/approval-state-machine.md`](architecture/approval-state-machine.md) — why a write cannot happen without a human saying yes, and why that survives a restart.
7. [`../eval/results/latest.md`](../eval/results/latest.md) — the latest eval run, as raw output.
8. [`architecture/observability.md`](architecture/observability.md) — what is traced, and a worked walkthrough of debugging a bad run.
9. [`runbooks/`](runbooks/) — running it locally, the evals, model providers, and troubleshooting.
10. [`media/README.md`](media/README.md) — the recorded run, frame by frame, and how to regenerate it.

## Conventions used in these documents

* Diagrams are ASCII so they survive a terminal, a diff and a copy-paste.
* Every claim about behaviour in these documents is backed by a test, an eval case
  or a command whose output is shown. Where something is **not** verified, the
  document says so explicitly rather than implying it.
* Estimates are labelled by whose time they describe: agent working time or human
  engineering days. They are not interchangeable.
