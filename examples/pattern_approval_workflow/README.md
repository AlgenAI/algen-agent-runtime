# Approval-gated workflow pattern

This runnable Runtime DAG performs a synthetic CRM lookup, creates a structured proposal, pauses once
for human input, and applies or rejects an idempotent synthetic update.

```text
lookup -> propose -> human checkpoint -> update
```

Run `python -m examples.pattern_approval_workflow.app` or import `agent.yaml` into Studio. The example
uses only Runtime workflow primitives; scheduling and checkpoint limits are not implemented in the
application. No real CRM mutation occurs.
