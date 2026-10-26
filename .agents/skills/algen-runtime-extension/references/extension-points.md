# Runtime extension points

## Plugins

Every code extension point below reaches a container through a plugin: a class with a
`PluginManifest` and `register(context)`, named in an agent's `domain_plugins` as `module:attribute`
or an `algen_agent_runtime.plugins` entry point, and loaded only from
`security.trusted_plugin_prefixes` (checked on module boundaries before import). `register` adds to
`context.tools`, `planners`, `contexts`, `verifiers`, `composers`, `semantics`, `methods`,
`model_services`, `providers`, `mcp_servers`, `policies`, `analytical_nodes` and `retrievers`; a
taken name raises `ConflictError`. Shared clients
go to `context.add_resource` and connect in `start()`, never at import or in `register`.

Every component must be describable without running: `describe_components` imports the plugin and
runs `register`, and must not need a secret, a network or a database. Declare the environment
variables a plugin reads in `PluginManifest.env`. Each kind is named from the settings by a field:
tools by `enabled_tools`, planners by `planning_strategy`, context builders by `context_builder`,
verifiers by `verification_policy.verifiers`, composers by `response_composer`, guardrail
policies by `guardrail_policy.policies` and `tool_policies`, search stores through their
`retrieval.<name>` context builder, providers by model profiles.
See `docs/plugins.md`.

## Model provider

Inspect `src/algen_agent_runtime/models/providers/`, the provider registry, and provider contract
tests. Normalize messages, tool calls, structured output, streaming, usage, retryable failures, and
capabilities. Provider credentials remain secret references. Document supported capabilities and
known limitations in `docs/provider-compatibility.md`.

Provider-specific strict JSON Schema subsets belong at the provider boundary. Preserve the original
application schema for Runtime validation; translate only the wire copy and cover nested objects,
optional/defaulted fields, constants, and unsupported constraint keywords in contract tests.

## Tool or connector adapter

Inspect `tools/contracts.py`, `tools/adapters.py`, the executor, and policy engine. Every tool needs a
strict input schema, output schema, permissions, side-effect class, idempotency class, timeout,
concurrency bound, and result-size bound. Use `ToolContext` for identity and secrets. A database
connector accepts parameterized statements through a deployment-owned handler; remote HTTP tools
must validate destinations and propagate the idempotency key.

`tools/authoring.py` builds those fields from typed functions (`@tool`, the presets, `Toolkit`);
a change to a preset's rules changes every tool built with it, so update `docs/tool-authoring.md`
and `tests/unit/test_tool_authoring.py` together. `ToolDefinition.requires_approval` may only add
approval; `ApprovalHandler` is a protocol only, and the Runtime ships no approval flow behind it.
`tools/testing.py` (`assert_tool_contract`, `fake_tool_context`) is the contract every tool is
tested against.

Email provider adapters live in `algen-agent-tools` (`algen_agent_tools.mail.EmailSender`;
`algen_agent_runtime.communications` is deprecated). They return the normalized,
content-minimal `EmailDeliveryReceipt`. Preserve the supplied idempotency key as the provider's
deduplication key or stable message identifier. Validate provider recipient refusals explicitly,
resolve attachments only through a tenant-authorized artifact loader, do not leak message bodies or
credentials in exceptions, and treat an uncertain post-submission failure as indeterminate through
the Runtime tool ledger. See `docs/email.md`.

## Retrieval backend

Inspect `retrieval/contracts.py`, `retrieval/vector_stores.py`, and `docs/rag.md`. Preserve tenant
filters, source provenance, score semantics, query/context limits, and prompt-injection boundaries.
Keep the backend SDK behind an optional extra. A store that is not one of the built-in `retrieval:`
types reaches a container through a plugin, `context.retrievers.register(name, store, **options)`,
which also registers its `retrieval.<name>` context builder.

## Guardrail policy and analytical handler

A project's own guardrail policy (`name`, `category`, `enforcement_mode`, `applies_to`,
`evaluate`) is registered with `context.policies.register(policy)`; the startup check fails when an
agent names a policy nothing registered, because the policy engine would otherwise skip it. The
analytical node kinds are fixed: a plugin supplies the deployment's `semantic_query` or
`model_call` handler with `context.analytical_nodes.register(kind, handler)` and cannot replace a
built-in handler. Adding a node kind is a Runtime change to `AnalyticalNodeKind`, the engine,
`describe` and the docs together.

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

Inspect `docs/framework-adapters.md` or `observability/`. External frameworks (LangGraph, OpenAI
Agents SDK, AutoGen, CrewAI) integrate via the **Application Registration** pattern:

- Application code constructs pre-configured external framework objects and registers them into the
  container using `build_container(framework_adapters=(adapter, ...))` or `container.frameworks.register(adapter)`.
- Never import framework graphs, crews, or agents from an arbitrary YAML dotted path.
- Framework dependencies remain optional and lazy.
- Governance boundary: Runtime governs outer tenancy, identity propagation, budgets, deadlines, and
  cancellation. Internal model or tool executions owned by foreign framework engines are not
  transparently governed by Runtime policy and must be observed/enforced via Traccia or native hooks.
- Telemetry integrations must remain optional and must not include content by default.
