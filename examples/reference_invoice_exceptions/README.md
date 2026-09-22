# Invoice exception handling

A synthetic accounts-payable workflow:

```text
document screen -> extraction -> [PO match || duplicate check] -> join -> policy route -> approval -> recommendation
```

The extractor has a structured schema and bounded repair. Runtime always presents the evidence and
exception route at a first-class approval checkpoint before recording a recommendation. The terminal
handler creates an idempotent recommendation and never executes payment.

### Running interactively

```bash
python -m examples.reference_invoice_exceptions.app
```

### Running non-interactively / automation

```bash
# Automatically approve recommendation
python -m examples.reference_invoice_exceptions.app --approve-all

# Automatically reject recommendation
python -m examples.reference_invoice_exceptions.app --reject-all

# Provide decision via answers file
python -m examples.reference_invoice_exceptions.app --answers-file path/to/answers.json
```

Or import this folder into Studio.

Production use requires reviewed document security, calibrated extraction confidence, immutable
evidence storage, real ERP authorization, and separation-of-duties enforcement.
