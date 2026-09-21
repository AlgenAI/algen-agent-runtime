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

Email provider adapters implement `communications.email.EmailSender` and return the normalized,
content-minimal `EmailDeliveryReceipt`. Preserve the supplied idempotency key as the provider's
deduplication key or stable message identifier. Validate provider recipient refusals explicitly,
resolve attachments only through a tenant-authorized artifact loader, do not leak message bodies or
credentials in exceptions, and treat an uncertain post-submission failure as indeterminate through
the Runtime tool ledger. See `docs/email.md`.

## Retrieval backend

Inspect `retrieval/contracts.py`, `retrieval/vector_stores.py`, and `docs/rag.md`. Preserve tenant
filters, source provenance, score semantics, query/context limits, and prompt-injection boundaries.
Keep the backend SDK behind an optional extra.

## Persistence store

Inspect the relevant Protocol and in-memory/PostgreSQL implementations. Preserve tenant-scoped keys,
atomic state transitions, optimistic concurrency where defined, timestamps, recovery behavior, and
forward schema migrations.

Workflow checkpoint stores additionally require detached reads, tenant isolation, compare-and-set
versions, manifest fingerprints, recoverable-state filtering, and corruption failure behavior. Run
the shared in-memory/PostgreSQL conformance suite when adding another backend. Parent/child workflow
recovery must reconcile the recorded child checkpoint; it must never dispatch a replacement child.

Artifact stores additionally preserve optional run association, content checksum and length,
scan/quarantine status, metadata-only reads, expiry visibility, bounded purge, and permanent
tenant-scoped deletion. Keep large-object SDKs optional and never return another tenant's descriptor
or bytes.

For S3-compatible implementations, keep authoritative metadata in a tenant-scoped durable store,
hash tenant identifiers in object keys, request service-side checksums and encryption, conditionally
create objects, verify downloads locally, compensate partial uploads, and make deletion/expiry safe
to retry. Credentials come from workload identity or the SDK chain, not Runtime YAML. Scanner
adapters return only `ArtifactScanResult`; lifecycle state and audit writes belong to
`ArtifactLifecycleService`.

## Framework or telemetry adapter

Inspect `docs/framework-adapters.md` or `observability/`. Runtime remains the governance envelope;
framework-native state must not bypass identity, policy, budgets, tool execution, or normalized
events. Telemetry integrations must remain optional and must not include content by default.
