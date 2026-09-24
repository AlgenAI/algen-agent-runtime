# Customer-support case resolution

A synthetic, Studio-importable enterprise workflow:

```text
triage -> [tenant knowledge || entitlement] -> response draft -> approval -> action record -> join
```

Refund requests pause for a missing order identifier. Knowledge retrieval is tenant-scoped, the CRM
and billing services are synthetic, and the final action records an idempotency key without issuing a
real refund. Runtime's first-class approval checkpoint gates the synthetic CRM write and retains the
reviewer decision.

### Running interactively

```bash
python -m examples.reference_customer_support.app "Please refund my order"
```

### Running non-interactively / automation

For guaranteed non-interactive automation, pass `--answers-file` with pre-recorded clarification and approval answers. Note that `--approve-all` resolves approval checkpoints only and fails if a workflow requests clarification.

```bash
# Non-interactive execution using the committed synthetic answers fixture:
python -m examples.reference_customer_support.app \
  --answers-file examples/reference_customer_support/fixtures/automation-answers.json
```

Where `automation-answers.json` contains:
```json
[
  {"type": "clarification", "answer": "ORD-1234"},
  {"type": "approval", "decision": "approved"}
]
```

Or import this folder into Studio.

Production adoption requires authenticated tenant mapping, reviewed PII policy, durable stores, real
connectors, durable Runtime checkpoints, and provider reconciliation before external writes.
