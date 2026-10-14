# The recorded run

Five screenshots of the agent working, produced by driving a real browser against the running
service. The assignment asks for "screenshots or a short recording of one run from start to
finish: a read, a proposed write, the approval, and a failure being handled" — these are those
four moments, with the page's opening state as a fifth.

They are **generated, not pasted together**. `devtasks screenshots` (or `make screenshots`) drives
Chrome over the DevTools Protocol and writes what it sees, so the images can be regenerated after
a UI change, and a reviewer can reproduce every one of them rather than taking a folder of PNGs on
trust. The driver is [`devtasks/chrome.py`](../../devtasks/chrome.py); the sequence is
[`devtasks/tasks.py`](../../devtasks/tasks.py).

| File | What it shows |
|---|---|
| [`01-loaded.png`](01-loaded.png) | The page at `localhost:8080/`, before anything is typed: user `alice`, a freshly minted session, and the note that reads run immediately while a write is proposed and waits. |
| [`02-read.png`](02-read.png) | **A read.** "find tickets about the printer" → `search_tickets {"q":"printer"}` → *1 of 1 matching ticket(s) on page 1*, in two steps. The call, its arguments and its result are all on screen. |
| [`03-proposed-write.png`](03-proposed-write.png) | **A proposed write, waiting.** The agent has proposed `create_ticket` and stopped. The card shows the exact arguments that would be sent, an expiry, and the two buttons. Nothing has been written — the ticket does not exist until the next frame. |
| [`04-approved-write.png`](04-approved-write.png) | **The approval, executed.** After clicking *Approve and run*: a `WRITE EXECUTED` turn reading *Created ticket TCK-1025.*, and the proposal card is gone. |
| [`05-failure-handled.png`](05-failure-handled.png) | **A failure, handled.** With the mock failing every request, the read comes back as a red `search_tickets` with *the ticketing API is rate limiting this client. This was attempted 4 times and has been given up on rather than retried further.* — and the agent says so in words rather than pretending it worked. |

## The eval output

[`eval-run.txt`](eval-run.txt) is the console transcript of the committed eval run — **19 of 19
cases passed in 87.7s** — recorded with `make eval` piped through `Tee-Object`. The same run's
formatted report is [`eval/results/latest.md`](../../eval/results/latest.md), and a summary table
is in the [README](../../README.md).

The transcript contains PowerShell's `NativeCommandError` banners interleaved with the real output.
That is not a failure: `uv` logs every HTTP request to stderr, and PowerShell wraps anything a
native command writes to stderr in that banner. The command exited 0 and the pass table is the
substance. The file is recorded as it happened rather than tidied, so it is evidence rather than a
retelling.

## How to regenerate them

The first four need a stack with a mock that works; the fifth needs a mock that does not. That is
why they are two runs rather than one — see the note below.

```bash
# 01–04 — a healthy stack
make up-ci              # or `make up`
make screenshots

# 05 — a mock that is genuinely failing
make down-ci
FLAKY=1 FLAKY_RATE=1.0 make up        # PowerShell: $env:FLAKY='1'; $env:FLAKY_RATE='1.0'
make screenshots ARGS=--failure
```

The task refuses to run if nothing answers at `/ping`, and it does not restart the mock to *create*
a failure: it captures a failure the stack really produced, or nothing.

## Why the failure is a separate run

With `FLAKY_RATE=1.0` every backend call fails, so the write proposed in the third frame could
never be executed — the approval in the fourth step would fail and the run would stop halfway.
A screenshot of an approval that cannot succeed would be a picture of the harness, not of the
product. The alternative — a mock that fails occasionally — cannot be relied on to fail during a
capture either, and would leave the most interesting frame present or absent at random.

## What the images are not

- **Not a live model.** The committed run uses the deterministic `scripted` provider, so the tool
  choices and wording in these frames are reproducible: the same commit produces the same run.
  A live-model run is a different exercise, and is discussed in
  [`docs/plan/stage-13.md`](../plan/stage-13.md).
- **Not a recording.** Screenshots, as the assignment permits as an alternative. A recording would
  need a screen recorder on the capture host, which CI does not have; the trade is a set of frames
  that can be regenerated anywhere Chrome or Edge is installed, including on a headless runner.
- **Not hand-edited.** Every pixel came from the browser. If a frame looks wrong, the fix is a
  code change and another `make screenshots`.
