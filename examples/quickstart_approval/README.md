# Human-checkpoint quickstart

Demonstrates a proposal, a bounded Runtime pause, resume, and an idempotent apply-or-abort handler.
Run `python -m examples.quickstart_approval.app` and answer `approve` or `reject`, or import the folder
into Studio and use its clarification checkpoint panel.

The write is synthetic. Production side effects should additionally use Runtime tool approval,
permissions, durable approval storage, and a real idempotency store.
