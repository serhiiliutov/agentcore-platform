"""The eval suite: how this agent's behaviour is measured rather than asserted.

An eval case is a scripted conversation with explicit expectations — which tools
must be called, which must *not* be called without approval, which facts must
survive a restart, which failures must be reported instead of retried. The unit
tests prove the parts work; the evals prove the agent behaves, end to end, through
the real HTTP and MCP boundaries.

Planned layout (built in Stage 13):

``cases.json``      the cases: prompt, scripted model turns, and assertions
``runner.py``       runs the suite against a live stack and writes a result file
``scoring.py``      each assertion type: tool_called, tool_not_called, ...
``report.py``       renders the run as the markdown table a reviewer reads
``results/``        committed results of the last run, as ``latest.md`` and
                    ``latest.json``

The runner exits non-zero when any case fails, so ``make eval`` is usable as a
gate in CI and not only as a report to read.
"""

__all__: list[str] = []
