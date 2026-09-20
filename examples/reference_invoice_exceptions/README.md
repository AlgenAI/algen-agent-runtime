# Invoice exception handling

A synthetic accounts-payable workflow:

```text
document screen -> extraction -> [PO match || duplicate check] -> join -> policy route -> checkpoint -> recommendation
```

The extractor has a structured schema and bounded repair. A documented amount variance causes a human
checkpoint. The terminal handler creates an idempotent recommendation and never executes payment.
Run `python -m examples.reference_invoice_exceptions.app` or import this folder into Studio.

Production use requires reviewed document security, calibrated extraction confidence, immutable
evidence storage, real ERP authorization, and separation-of-duties enforcement.
