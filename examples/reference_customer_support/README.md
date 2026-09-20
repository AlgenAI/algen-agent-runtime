# Customer-support case resolution

A synthetic, Studio-importable enterprise workflow:

```text
triage -> [tenant knowledge || entitlement] -> response draft -> action record -> join
```

Refund requests pause for a missing order identifier. Knowledge retrieval is tenant-scoped, the CRM
and billing services are synthetic, and the final action records an idempotency key without issuing a
real refund. Run `python -m examples.reference_customer_support.app` or import this folder into Studio.

Production adoption requires authenticated tenant mapping, reviewed PII policy, durable stores, real
connectors, and Runtime tool approval before external writes.
