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

```bash
# Non-refund case with automated approval
python -m examples.reference_customer_support.app "How do I update my profile?" --approve-all

# Refund case with ordered clarification and approval answers
python -m examples.reference_customer_support.app --answers-file path/to/answers.json
```

Where `answers.json` contains:
```json
[
  {"type": "clarification", "answer": "ORD-1234"},
  {"type": "approval", "decision": "approved"}
]
```

Or import this folder into Studio.

Production adoption requires authenticated tenant mapping, reviewed PII policy, durable stores, real
connectors, durable Runtime checkpoints, and provider reconciliation before external writes.
