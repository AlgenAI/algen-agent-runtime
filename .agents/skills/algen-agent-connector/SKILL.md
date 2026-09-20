---
name: algen-agent-connector
description: Connect an Algen Runtime agent or workflow to typed tools, MCP, HTTP APIs, databases, retrieval stores, model providers, memory, persistence, or telemetry without bypassing governance.
---

# Connect an Algen agent

Identify the requested systems, data direction, side effects, credentials, network boundary,
tenancy, and expected failure behavior. Read the matching parts of `references/connectors.md` and
combine connectors only when the use case needs them.

Expose external actions as typed Runtime tools or workflow resources. Do not call a connector
directly from orchestration code to bypass policy, approvals, idempotency, audit events, budgets, or
telemetry. Use secret references in configuration and resolve values only at execution time.

For each connection, declare least-privilege permissions, read/write behavior, idempotency, strict
schemas, timeout, retry policy, concurrency, result-size limit, and allowed destinations. Mark it on
the consuming workflow node with a `WorkflowResourceReference` so Studio can display the actual
topology.

Test with a deterministic fake or local fixture. Live credentials and paid services belong only in
explicit live tests. Document the required Runtime extra and environment variable names without
including secret values.

