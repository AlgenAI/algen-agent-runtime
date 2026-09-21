# Approval-gated workflow pattern

This runnable Runtime DAG performs a synthetic CRM lookup, creates a structured proposal, pauses at a
first-class approval node, and applies approved or modified parameters through an idempotent synthetic
update. Rejection safely skips the update.

```text
lookup -> propose -> approval -> update
```

Run `python -m examples.pattern_approval_workflow.app` or import `agent.yaml` into Studio. The example
uses only Runtime workflow primitives; scheduling and checkpoint limits are not implemented in the
application. No real CRM mutation occurs.
