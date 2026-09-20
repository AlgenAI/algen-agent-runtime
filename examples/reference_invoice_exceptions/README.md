# reference_invoice_exceptions

**Tier:** Reference Application | **Status:** 🚧 Placeholder — not yet implemented

## Enterprise problem

Finance teams process high-volume documents but require evidence and separation of duties before payment.

## Planned scope

**Shape:** Document intake plus specialist agents for extraction, purchase-order matching, duplicate/fraud signals, policy checks, and payment recommendation.

**What it should prove:**

- Hostile-document scanning before processing.
- Structured extraction from invoice documents.
- Deterministic reconciliation against purchase orders.
- Confidence thresholds for exception routing.
- Policy-driven routing to human reviewers.
- Human approval before any payment recommendation.
- Immutable evidence: every decision is traceable.
- ERP write idempotency.

## Implementation checklist

- [ ] Define Pydantic models in `application/models.py` (invoice, PO, exception, payment-rec).
- [ ] Implement document intake and extraction tools in `application/tools.py`.
- [ ] Implement PO-matching and policy-check logic in `application/policies.py`.
- [ ] Implement the exception-routing workflow in `application/workflow.py`.
- [ ] Create `config/agent.yaml`.
- [ ] Populate `data/` with synthetic invoice and PO fixtures.
- [ ] Create `manifest.yaml`.
- [ ] Create `tests/` covering extraction, reconciliation, policy routing, and approval.
- [ ] Update this README with architecture, setup/run/test/reset, and production gaps.

## Production gaps (anticipated)

- Synthetic ERP adapter only — no real SAP, Oracle, or similar integration.
- Document scanning is pattern-based; production requires a reviewed hostile-document policy.
- Confidence thresholds need calibration against real invoice variance.
