# pattern_approval_workflow

**Tier:** Pattern | **Status:** ⚠️ Work in progress — not yet runnable

> [!WARNING]
> This example is under active development. The agent configuration previously declared `crm.lookup` and `crm.update` as enabled tools, and declared a `workflow` graph — none of which are registered or executed by the current runtime. These have been removed from `agent.yaml` with TODO comments. Publish this example only when domain tools are registered and the workflow graph is actually executed.

## What this example will demonstrate

- Approval-gated workflow: the agent proposes a CRM update, pauses for human approval, and executes only if approved.
- One visible pause, decision (approve/reject), and resume path.
- `approval_policy` enforcement: side-effecting tools require approval before execution.
- Non-idempotent tool protection and cancellation support.

## Implementation checklist

- [ ] Implement `crm.lookup` (read-only) and `crm.update` (write, side-effect) tools.
- [ ] Register tools via `container.tools.register()` in `app.py`.
- [ ] Re-enable `enabled_tools` and `tool_permissions` in `agent.yaml`.
- [ ] Implement the workflow graph in `workflow.py` (or use the runtime's native workflow once available).
- [ ] Add an approval queue UI or CLI flow showing the pause → decision → resume path.
- [ ] Add `tests/test_smoke.py` covering approval-granted and approval-rejected paths.

## Production gaps

- No tools are registered: the agent cannot look up or update CRM records.
- The workflow graph is not executed by the runtime (commented out in `agent.yaml`).
- No approval queue UI exists.
