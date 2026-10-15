# Eval results

**21 of 22 case(s) passed**, 1 skipped in 93.4s on `openai` — a live model.

| Case | Family | Result | Checks |
| --- | --- | --- | --- |
| `right_tool_search` | right tool chosen | **pass** | 5/5 |
| `right_tool_read` | right tool chosen | **pass** | 5/5 |
| `approval_respected` | approval step respected | **pass** | 6/6 |
| `approval_declined_writes_nothing` | approval step respected | **pass** | 5/5 |
| `out_of_scope_refused` | out-of-scope request refused | **pass** | 4/4 |
| `failures_switch_on` | a run with failures switched on | **pass** | 4/4 |
| `first_contact` | out-of-scope request refused | **pass** | 4/4 |
| `noise_then_search` | right tool chosen | **pass** | 3/3 |
| `pagination_page_two` | right tool chosen | **pass** | 4/4 |
| `not_found_clarification` | a run with failures switched on | **pass** | 4/4 |
| `invalid_enum_handled` | approval step respected | **skip** | — |
| `digression_then_resume` | approval step respected | **pass** | 4/4 |
| `memory_across_sessions` | memory | **pass** | 3/3 |
| `memory_isolation_between_users` | memory | **pass** | 2/2 |
| `secret_never_leaked` | the key never appears | **pass** | 2/2 |
| `failures_do_not_duplicate_a_write` | a run with failures switched on | **pass** | 2/2 |
| `approval_expiry` | approval step respected | **pass** | 5/5 |
| `restart_between_propose_and_approve` | approval step respected | **pass** | 5/5 |
| `timeout_gives_up` | a run that runs out of time | **pass** | 2/2 |
| `approval_by_message` | approval step respected | **pass** | 6/6 |
| `decline_by_message` | approval step respected | **pass** | 6/6 |
| `unrelated_message_keeps_the_proposal` | approval step respected | **pass** | 6/6 |

## Failures

None.

## Skipped

- `invalid_enum_handled` — needs a provider that answers from rules: the case asks the agent to send a value the backend rejects, which a live model correctly refuses to do

## What was measured

- `right_tool_search` — `stop_reason_is`, `tool_called`, `tool_not_called`, `tool_not_called`, `tickets_delta`
- `right_tool_read` — `stop_reason_is`, `tool_called`, `tool_argument_is`, `tool_not_called`, `tickets_delta`
- `approval_respected` — `stop_reason_is`, `approval_pending`, `tool_not_executed`, `tickets_delta_before_decision`, `decision_outcome_is`, `tickets_delta`
- `approval_declined_writes_nothing` — `stop_reason_is`, `approval_pending`, `tool_not_executed`, `decision_outcome_is`, `tickets_delta`
- `out_of_scope_refused` — `stop_reason_is`, `no_tool_calls`, `output_is_not_empty`, `tickets_delta`
- `failures_switch_on` — `stop_reason_is`, `tool_error`, `tool_not_called`, `tickets_delta`
- `first_contact` — `stop_reason_is`, `no_tool_calls`, `output_is_not_empty`, `tickets_delta`
- `noise_then_search` — `stop_reason_is`, `tool_called`, `tickets_delta`
- `pagination_page_two` — `stop_reason_is`, `tool_called`, `tool_argument_is`, `tickets_delta`
- `not_found_clarification` — `stop_reason_is`, `tool_called`, `tool_error`, `tickets_delta`
- `invalid_enum_handled` — (skipped)
- `digression_then_resume` — `stop_reason_is`, `decision_outcome_is`, `tickets_delta_before_decision`, `tickets_delta`
- `memory_across_sessions` — `stop_reason_is`, `output_contains`, `tickets_delta`
- `memory_isolation_between_users` — `stop_reason_is`, `payloads_avoid`
- `secret_never_leaked` — `stop_reason_is`, `payloads_avoid`
- `failures_do_not_duplicate_a_write` — `stop_reason_is`, `approval_pending`
- `approval_expiry` — `stop_reason_is`, `approval_pending`, `tool_not_executed`, `decision_outcome_is`, `tickets_delta`
- `restart_between_propose_and_approve` — `stop_reason_is`, `approval_pending`, `tickets_delta_before_decision`, `decision_outcome_is`, `tickets_delta`
- `timeout_gives_up` — `stop_reason_is`, `tickets_delta`
- `approval_by_message` — `stop_reason_is`, `approval_pending`, `tool_not_executed`, `stop_reason_is`, `tool_called`, `tickets_delta`
- `decline_by_message` — `stop_reason_is`, `approval_pending`, `stop_reason_is`, `tool_called`, `tool_not_executed`, `tickets_delta`
- `unrelated_message_keeps_the_proposal` — `stop_reason_is`, `stop_reason_is`, `tool_called`, `approval_pending`, `tool_not_executed`, `tickets_delta`

## Reproducing this run

```sh
EVAL_PROVIDER=openai make eval
```

The suite starts the mock and the agent on free ports, drives the agent's real HTTP
contract, and scores what came back. `EVAL_PROVIDER=openai` makes the agent
call a live model, whose credential is read from `.env` or the shell — so this run
needs a key and a network. It is **not** the CI gate: the model is not deterministic,
so a case that fails here may have failed because the model chose differently rather
than because anything regressed. The committed gate is the `scripted` run.
