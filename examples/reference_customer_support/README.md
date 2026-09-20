# reference_customer_support

**Tier:** Reference Application | **Status:** 🚧 Placeholder — not yet implemented

> [!IMPORTANT]
> This is planned as the **first enterprise reference application**. It is intentionally scaffolded before implementation so the folder structure, ownership boundaries, and planned scope are visible in the repository.

## Enterprise problem

Support teams need grounded, citation-backed answers and safe account changes across CRM, billing, and ticketing systems — without autonomous write access.

## Planned scope

**Shape:** Start as one agent, then show an optional multi-agent version with triage, knowledge retrieval, entitlement checking, response drafting, and action execution.

**What it should prove:**

- Tenant-scoped RAG with PII redaction and verified citations.
- Tool permissions: read-only CRM lookup, write-gated refund/account actions.
- Approval before any refund or account change.
- Idempotent writes with audit evidence.
- Escalation and conversation continuity.
- Evaluation against a synthetic ticket set.

## Folder structure

```text
reference_customer_support/
├── README.md
├── __init__.py
├── app.py                    # CLI/API composition root
├── dashboard.py              # Optional host UI; no business decisions
├── manifest.yaml
├── application/
│   ├── models.py             # Pydantic request, response, state, and event models
│   ├── tools.py              # CRM, billing, and ticketing tool registrations
│   ├── policies.py           # Domain authorization and redaction policies
│   └── workflow.py           # Business workflow topology
├── config/
│   ├── agent.yaml
│   └── evaluation.yaml
├── data/                     # Synthetic ticket fixtures and golden evaluation set
├── database/
│   └── migrations/           # Application-owned schema only
├── docs/                     # Architecture, threat assumptions, production gaps
├── tests/
│   ├── test_smoke.py
│   ├── test_tools.py
│   ├── test_workflow.py
│   ├── test_policies.py
│   └── test_evaluation.py
└── ui/                       # Domain-specific UI assets only
```

## Implementation checklist

- [ ] Define Pydantic models in `application/models.py`.
- [ ] Implement typed CRM, billing, and ticketing tools in `application/tools.py`.
- [ ] Define authorization and redaction policies in `application/policies.py`.
- [ ] Implement the business workflow in `application/workflow.py`.
- [ ] Create `config/agent.yaml` with triage and response-drafting agents.
- [ ] Create `config/evaluation.yaml` with synthetic ticket fixtures and thresholds.
- [ ] Populate `data/` with synthetic, redistributable ticket fixtures.
- [ ] Create `app.py` and `dashboard.py`.
- [ ] Create `manifest.yaml`.
- [ ] Create all tests in `tests/`.
- [ ] Update this README with architecture diagram, setup/run/test/reset commands, and production gaps.

## Production gaps (anticipated)

- Synthetic CRM, billing, and ticketing adapters — no real integrations.
- No authentication or real tenant isolation.
- Approval queue requires a UI or CLI consumer.
- PII redaction is configuration-driven; production requires a reviewed redaction policy per tenant.
