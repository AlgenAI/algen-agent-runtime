# Side-effect reconciliation

What to do when a write's outcome is unknown: the request left, then the connection timed out.
The Runtime never retries such a call by itself. This example shows the whole path offline, with
a mock model and an in-process fake billing system:

- `billing.issue_credit` (`tools.py`, registered by `BillingPlugin` in `plugin.py`) is an external
  write keyed by the call's idempotency key, with a `reconcile` hook that looks the key up in the billing system and a `compensation` that
  voids the credit.
- `tool_policy.on_indeterminate: pause` (`agent.yaml`) pauses the run in `awaiting_reconciliation`
  instead of failing it, and settling the call continues the run.
- Four runs:
  1. The credit is applied, then the response times out. An operator lists the unknown calls
     (`runtime.reconciliation.list`) and asks the tool (`auto_reconcile`); the hook finds the
     credit and the run resumes with the recorded result.
  2. The request times out before the credit is applied. Over the HTTP API, in-process through
     `httpx.ASGITransport`: `GET /v1/tool-executions` needs `tools:reconcile`; `POST .../reconcile`
     answers `unknown`; the operator `POST .../resolve`s it `failed` with a reason, recorded with
     their user id as the actor, and the run resumes (the model hears `ToolCallNotApplied`).
  3. The operator asks for the first credit to be compensated: the ledger records
     `compensating`, then `compensated`. The Runtime never compensates by itself.
  4. The tool refuses a locked account with `ToolNotAttemptedError` before sending anything: the
     call is recorded `failed`, not `indeterminate`, and there is nothing to reconcile.

The HTTP part uses `development_headers` authentication, which is for loopback development only;
see [`pattern_secured_api`](../pattern_secured_api/README.md) for the API behind OIDC.

## Run it

```bash
python -m examples.pattern_side_effect_reconciliation.app
```

No API key, network or database is needed. From a source checkout without installing, use
`PYTHONPATH=src:. python -m examples.pattern_side_effect_reconciliation.app`.

## Expected output

Keys are random per run; the rest is deterministic:

```text
== 1. The credit was applied, then the response timed out ==
run status: awaiting_reconciliation
pause: kind=tool_reconciliation tool=billing.issue_credit key=<run id>:<tool call id>
operator lists unknown calls: [('billing.issue_credit', 'indeterminate')]
reconcile hook: outcome=completed run_resumed=True result_reference=cr-0001
run status: completed; output: Credited acct-1001 with 25.00.
events: ['tool.reconciliation_required', 'tool.reconciled', 'tool.completed']

== 2. The request timed out before it was applied (operator over HTTP) ==
run status: awaiting_reconciliation
GET /v1/tool-executions without tools:reconcile -> 403
GET /v1/tool-executions -> billing.issue_credit indeterminate (error ToolExecutionError, has_result False)
POST .../reconcile -> outcome=unknown
POST .../resolve -> outcome=failed run_resumed=True actor=ops-maria reason='billing support confirms no credit for acct-2002'
run status: completed; output: The credit did not go through; nothing was charged.

== 3. Compensation, only when an operator asks ==
compensate: outcome=compensated status=compensated
ledger decisions: ['reconcile:completed', 'compensate:compensating', 'compensate:compensated']
billing voided credits: ['cr-0001']

== 4. Refused before anything was sent (ToolNotAttemptedError) ==
run status: completed
ledger: billing.issue_credit failed (error ToolNotAttemptedError)
requests sent to billing: 0
left to reconcile: 0
```

## In production

- Use a durable tool-execution store (`storage.tool_execution_store: postgres`) so the ledger and
  paused runs survive a restart; the in-memory stores here do not.
- Give `tools:reconcile` only to operators. Every decision is compare-and-set on the ledger status
  (`expected_status` returns 409 if someone changed the call since you looked), audited with the
  actor and reason, and announced as a content-free run event.
- A `reconcile` hook must only read: it may run more than once, including when a recovered run
  replays a call that was in flight when the process stopped.
- Make compensation idempotent by the idempotency key; after a crash an operator may request it
  again.

## Read next

- [Resolving indeterminate tool calls](../../docs/operations.md#resolving-indeterminate-tool-calls)
  (runbook, rules, events and audit records)
- [Unknown outcomes: reconcile and compensation](../../docs/tool-authoring.md#unknown-outcomes-reconcile-and-compensation)
- [`tool_policy.on_indeterminate`](../../docs/agent-tooling.md)
