# Runtime extension points

## Model provider

Inspect `src/algen_agent_runtime/models/providers/`, the provider registry, and provider contract
tests. Normalize messages, tool calls, structured output, streaming, usage, retryable failures, and
capabilities. Provider credentials remain secret references. Document supported capabilities and
known limitations in `docs/provider-compatibility.md`.

## Tool or connector adapter

Inspect `tools/contracts.py`, `tools/adapters.py`, the executor, and policy engine. Every tool needs a
strict input schema, output schema, permissions, side-effect class, idempotency class, timeout,
concurrency bound, and result-size bound. Use `ToolContext` for identity and secrets. A database
connector accepts parameterized statements through a deployment-owned handler; remote HTTP tools
must validate destinations and propagate the idempotency key.

## Retrieval backend

Inspect `retrieval/contracts.py`, `retrieval/vector_stores.py`, and `docs/rag.md`. Preserve tenant
filters, source provenance, score semantics, query/context limits, and prompt-injection boundaries.
Keep the backend SDK behind an optional extra.

## Persistence store

Inspect the relevant Protocol and in-memory/PostgreSQL implementations. Preserve tenant-scoped keys,
atomic state transitions, optimistic concurrency where defined, timestamps, recovery behavior, and
forward schema migrations.

## Framework or telemetry adapter

Inspect `docs/framework-adapters.md` or `observability/`. Runtime remains the governance envelope;
framework-native state must not bypass identity, policy, budgets, tool execution, or normalized
events. Telemetry integrations must remain optional and must not include content by default.

