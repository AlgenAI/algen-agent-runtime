"""Algen Agent Runtime examples kept outside the reusable core package.

Portfolio tiers
---------------
Tier 1 — Quickstarts (prefix: quickstart_)
    quickstart_local_chat      Local Ollama agent; no cloud credentials required
    quickstart_rag             Retrieval-augmented generation with citation verification
    quickstart_cloud_chat      Smallest hosted-provider path
    quickstart_tool            Typed read-only workflow resource
    quickstart_agent_tool      Typed @tool functions in a Toolkit, registered by a plugin
    quickstart_approval        Human checkpoint, resume, and apply-or-abort
    quickstart_conversation    Streamed conversation turns, steering, tool notes and approvals

Tier 2 — Focused patterns (prefix: pattern_)
    pattern_provider_fallback          Multi-provider fallback with routing evidence
    pattern_guarded_tools              Prechecks, tool-call guards, scoped cache and usage meter
    pattern_governed_research          Evidence-oriented research with citations
    pattern_approval_workflow          Approval-gated CRM workflow
    pattern_text_to_sql                Read-only Text-to-SQL; advanced/ adds pgvector schema retrieval
    pattern_langgraph_governance       Graph execution through the Runtime adapter
    pattern_openai_agents_integration  An OpenAI Agents SDK agent behind the Runtime adapter
    pattern_mcp_governance             MCP server tools with operator policy overrides
    pattern_multi_agent_fanout         Bounded dynamic agent fan-out and join
    pattern_child_workflow             Exact-version parent/child workflow dispatch
    pattern_evaluation_gate            Evaluation fixtures and promotion gate

Tier 3 — Enterprise reference applications (prefix: reference_)
    reference_customer_support         Customer support case resolution
    reference_incident_response        IT incident triage and remediation
    reference_invoice_exceptions       Accounts-payable invoice exception handling

Case studies (prefix: case_study_)
    case_study_responsible_hiring      Responsible-AI hiring reference (formerly hiring_agent)
    case_study_teaching_assistant      Policy-extension teaching example (formerly virtual_teaching_assistant)

Context engineering (single modules)
    context_compaction, context_evaluation, context_offloading, learned_memory,
    progressive_skills, working_state

Every runnable example runs offline in CI (tests/integration/test_all_examples_run.py), with
``--offline`` for those that call a hosted provider (see ``offline.py``), unless it is listed
there as needing a service such as Ollama or pgvector.

Naming conventions
------------------
- Directory names: lowercase snake_case, prefixed by tier.
- Agent IDs: stable kebab-case (e.g. customer-support-triage).
- Tools: namespaced verb-object IDs (e.g. crm.get_case, billing.issue_refund).
- Pydantic models: PascalCase; Python modules/tools: snake_case.
- Environment variables: upper snake_case scoped to the application.
"""
