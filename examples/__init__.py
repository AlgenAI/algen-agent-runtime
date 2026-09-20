"""Algen Agent Runtime examples kept outside the reusable core package.

Portfolio tiers
---------------
Tier 1 — Quickstarts (prefix: quickstart_)
    quickstart_local_chat      Local Ollama agent; no cloud credentials required
    quickstart_rag             Retrieval-augmented generation with citation verification
    quickstart_cloud_chat      (placeholder) Smallest hosted-provider path
    quickstart_tool            (placeholder) Typed read-only tool before introducing writes
    quickstart_approval        (placeholder) Approval pause, decision, resume, and rejection

Tier 2 — Focused patterns (prefix: pattern_)
    pattern_provider_fallback      Multi-provider fallback with routing evidence
    pattern_governed_research      Evidence-oriented research with citations (WIP)
    pattern_approval_workflow      Approval-gated CRM workflow (WIP)
    pattern_text_to_sql            Read-only Text-to-SQL; advanced/ adds pgvector schema retrieval
    pattern_langgraph_governance   (placeholder) LangGraph wrapped by Traccia governance
    pattern_multi_agent_fanout     (placeholder) Two-agent sequence and parallel fan-out
    pattern_evaluation_gate        (placeholder) Evaluation fixtures and promotion gate

Tier 3 — Enterprise reference applications (prefix: reference_)
    reference_customer_support         (placeholder) Customer support case resolution
    reference_incident_response        (placeholder) IT incident triage and remediation
    reference_invoice_exceptions       (placeholder) Accounts-payable invoice exception handling

Case studies (prefix: case_study_)
    case_study_responsible_hiring      Responsible-AI hiring reference (formerly hiring_agent)
    case_study_teaching_assistant      Policy-extension teaching example (formerly virtual_teaching_assistant)

Naming conventions
------------------
- Directory names: lowercase snake_case, prefixed by tier.
- Agent IDs: stable kebab-case (e.g. customer-support-triage).
- Tools: namespaced verb-object IDs (e.g. crm.get_case, billing.issue_refund).
- Pydantic models: PascalCase; Python modules/tools: snake_case.
- Environment variables: upper snake_case scoped to the application.
"""
