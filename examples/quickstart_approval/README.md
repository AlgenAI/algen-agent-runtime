# Human-checkpoint quickstart

Demonstrates a proposal, a first-class Runtime approval node, durable approve/modify/reject decision,
expiry, and an idempotent apply-or-abort handler.

### Running interactively

```bash
python -m examples.quickstart_approval.app
```
When prompted, enter `approve` (or `yes`) to approve, or `reject` to reject.

### Running non-interactively / automation

For CI pipelines, scripts, and headless demos:

```bash
# Deterministically approve
python -m examples.quickstart_approval.app --approve-all

# Deterministically reject
python -m examples.quickstart_approval.app --reject-all

# Provide ordered decisions from a JSON file
python -m examples.quickstart_approval.app --answers-file path/to/answers.json
```

Or import the folder into Studio and use its approval checkpoint panel.

The write is synthetic. Production side effects must still use permissions, a durable workflow store,
a real idempotency store, and provider reconciliation for ambiguous outcomes.
