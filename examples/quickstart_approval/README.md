# Human-checkpoint quickstart

Demonstrates a proposal, a first-class Runtime approval node, durable approve/modify/reject decision,
expiry, and an idempotent apply-or-abort handler. Run
`python -m examples.quickstart_approval.app` and answer `approve` or `reject`, or import the folder
into Studio and use its approval checkpoint panel.

The write is synthetic. Production side effects must still use permissions, a durable workflow store,
a real idempotency store, and provider reconciliation for ambiguous outcomes.
