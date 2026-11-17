# Runtime extension points

## Plugins

Every code extension point below reaches a container through a plugin: a class with a
`PluginManifest` and `register(context)`, named in an agent's `domain_plugins` as `module:attribute`
or an `algen_agent_runtime.plugins` entry point, and loaded only from
`security.trusted_plugin_prefixes` (checked on module boundaries before import; for an entry point,
since 0.2.0b4, against the module it imports, and a name that two distributions declare is refused). `register` adds to
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
capabilities. Decode tool-call arguments with `tool_arguments` from
`models/providers/openai_compatible.py`, so a malformed or cut-off call becomes a retryable
`InvalidToolCallError` instead of a crashed run. Report `images` and `files` capabilities only
for input the adapter really sends as image or file parts. Provider credentials remain secret references,
and a default `env://<VENDOR>_API_KEY` goes only to the vendor's own endpoint: since 0.2.0b4 a
built-in hosted type (`gemini`, `xai`, `groq`, `mistral`; since 0.2.0b5 also `openai`,
`anthropic`, `deepseek`, `huggingface_inference`) with another `base_url` needs an explicit
`api_key`, and a new provider type should do the same: honour `base_url` and never send the
default key elsewhere. Cancellation must reach the wire: when the awaiting task is cancelled, abort
the HTTP request and close a streamed response before re-raising `CancelledError` (do not retry or
swallow it), as the built-in adapters do since 0.2.0b5; `tests/unit/test_provider_cancellation.py`
and `tests/fake_model_server.py` show how to test it. Map HTTP errors by status the same way in
`generate`, `stream` and `embed` (subclasses of `OpenAICompatibleProvider` get this from
`_http_error`; override `_error_kind_for` for a vendor whose statuses differ, as Gemini's 400
`API_KEY_INVALID` is `authentication`). An error the vendor reports inside a stream after HTTP 200
must raise a `ProviderError` (`stream_error` and `stream_error_kind` in `openai_compatible.py`),
never end the stream as completed with partial text; the router then raises it without falling
back and the run retracts the streamed text (since 0.2.0b5). `embed` needs a timeout and turns
httpx errors into `ProviderError`. Check every claimed capability with the live provider
contract suite (`tests/live`, `ALGEN_LIVE_FAKE=1` offline). Document supported capabilities and
known limitations in `docs/provider-compatibility.md`.

Provider-specific strict JSON Schema subsets belong at the provider boundary. Preserve the original
application schema for Runtime validation; translate only the wire copy and cover nested objects,
optional/defaulted fields, constants, and unsupported constraint keywords in contract tests.

## Tool or connector adapter

Inspect `tools/contracts.py`, `tools/adapters.py`, the executor, and policy engine. Every tool needs a
strict input schema, output schema, permissions, side-effect class, idempotency class, timeout,
concurrency bound, and result-size bound. Use `ToolContext` for identity and secrets, and
`ToolContext.credentials` for the caller's own credentials when the tool acts as the signed-in
user: a read-only, memory-only mapping that never enters `repr`, dumps, caches or idempotency keys,
and is empty after a resume without credentials or a recovery. A tool may also carry a `precheck`
and be marked `volatile` (docs/agent-tooling.md, "Tool-call guards"). A database
connector accepts parameterized statements through a deployment-owned handler; remote HTTP tools
must validate destinations and propagate the idempotency key.

`tools/authoring.py` builds those fields from typed functions (`@tool`, the presets, `Toolkit`);
a change to a preset's rules changes every tool built with it, so update `docs/tool-authoring.md`
and `tests/unit/test_tool_authoring.py` together. `ToolDefinition.requires_approval` may only add
approval, and `ToolExecutor.execute` refuses such a tool unless it is given a `ToolApproval`
bound to the call (`ToolApproval.for_call(...)`: tool, version and a digest of the arguments);
a call the approval does not cover raises `ApprovalMismatchError`. Code that calls the executor
directly proves the decision itself (docs/tool-authoring.md, "Who proves an approval is valid").
`ApprovalHandler` is a protocol only, and the Runtime ships no approval flow behind it.
A custom `ApprovalService` takes `decided_by` and `comment` keywords on `decide` and records them
with `decided_at` (the HTTP route passes the principal's user id; `Runtime.resume(...,
decided_by=...)` for hosts). One without them still works but records no approver, and the
Runtime logs `approval_service_records_no_approver` once. From 0.2.0b4 `decide` must refuse any
decision but `approved`, `rejected` and `modified`, and `modified_parameters` without `modified`:
call `algen_agent_runtime.approvals.service.check_decision(decision, modified_parameters)` first,
as the built-in services do. The Runtime refuses such decisions before calling `decide`, and a
service that returns any status other than approved or modified approves nothing.
An agent registry used by the Runtime (`InMemoryAgentRegistry`) keeps an owner per agent name
from 0.2.0b4: `register(definition, owner=...)` refuses a version of a name another owner
registered, and paused or recovered runs resolve the version they started with
(`config.registry.pinned_agent(registry, state)`).
A synchronous handler runs in the bounded tool thread pool; `execution="process"` runs a
top-level, picklable handler in a subprocess that is killed at its timeout.
To let tools inside a foreign framework stay governed, hand them over with
`frameworks.GovernedToolBridge`; the framework's own tools and model calls are not governed.
Since 0.2.0b4 the bridge checks each call as `plan_tool` would: under its `approval_policy`
(default `ApprovalPolicy()`) a `write`, `external` or `destructive` tool needs an approval the
framework cannot ask for, so it is refused with `PolicyDeniedError`; pass `policies=
container.runtime.policies` to apply the deployment's `plan_tool` policies too.
A tool that refuses before sending anything raises `ToolNotAttemptedError`, so the ledger records
`failed`, not `indeterminate`; a toolkit's client factory raises `ConfigurationError` for a
missing setting.
`tools/testing.py` (`assert_tool_contract`, `fake_tool_context`) is the contract every tool is
tested against.

Email provider adapters live in `algen-agent-tools` (`algen_agent_tools.mail.EmailSender`;
the Runtime's `communications` package was removed in 0.2). They return the normalized,
content-minimal `EmailDeliveryReceipt`. Preserve the supplied idempotency key as the provider's
deduplication key or stable message identifier. Validate provider recipient refusals explicitly,
resolve attachments only through a tenant-authorized artifact loader, do not leak message bodies or
credentials in exceptions, and treat an uncertain post-submission failure as indeterminate through
the Runtime tool ledger. See `docs/email.md`.

## Retrieval backend

Inspect `retrieval/contracts.py`, `retrieval/vector_stores.py`, and `docs/rag.md`. Preserve tenant
filters, source provenance, score semantics, query/context limits, and prompt-injection boundaries.
Keep the backend SDK behind an optional extra. Since 0.2.0b4 the persistent built-in stores refuse
(`ValueError`, before embedding anything) a document without `tenant_id` metadata; a new
persistent store should do the same rather than file it under a shared tenant. A store that is not one of the built-in `retrieval:`
types reaches a container through a plugin, `context.retrievers.register(name, store, **options)`,
which also registers its `retrieval.<name>` context builder.

## Guardrail policy and analytical handler

A project's own guardrail policy (`name`, `category`, `enforcement_mode`, `applies_to`,
`evaluate`) is registered with `context.policies.register(policy)`; the startup check fails when an
agent names a policy nothing registered, because the policy engine would otherwise skip it. A
policy registered with `mandatory=True` (like `authorization` and `side_effects`) fails closed when
it raises or times out, whatever `policies.on_error` says (since 0.2.0b4). A text-matching policy
should match on `algen_agent_runtime.security.fold_text(text)` too, as the built-in
ones do since 0.2.0b4, so zero-width characters and lookalike letters cannot slip past it. Reuse
`redact_secrets`, `redact_pii`, `redact_secret_text` and `is_sensitive_key` from
`algen_agent_runtime.security` instead of copying their patterns. Since 0.2.0b4 an `after_model`
or `final_response` policy also sees streamed answer text before it is sent as `model.delta`: a
plain `str` window with `context["streaming"] = True`, checked many times per answer. It must
accept a string, return `redact` or `transform` with a string to mask it, and anything other
than allow withholds the rest of the stream (`model.delta.retracted`); these per-window checks
are not audited, the complete answer is. The
in-memory tool-execution ledger raises `ConflictError` for another tenant's idempotency key, as the
PostgreSQL one does; a custom ledger must never return or replace another tenant's record. The
analytical node kinds are fixed: a plugin supplies the deployment's `semantic_query` or
`model_call` handler with `context.analytical_nodes.register(kind, handler)` and cannot replace a
built-in handler. Adding a node kind is a Runtime change to `AnalyticalNodeKind`, the engine,
`describe` and the docs together. Since 0.2.0b5 the analytical graph engine (`analytical_nodes`)
and remote model services (`model_services`) are marked experimental: they can change in a minor
release without a deprecation window (docs/api-stability.md, "Experimental interfaces"); check
with `algen_agent_runtime.stability.is_experimental`.

## Persistence store

Inspect the relevant Protocol and in-memory/PostgreSQL implementations. Preserve tenant-scoped keys,
atomic state transitions, optimistic concurrency where defined, timestamps, recovery behavior, and
forward schema migrations.

A session memory store should also implement `SessionOwnership.claim_session(tenant_id,
session_id, user_id) -> bool` (as the in-memory, Redis and PostgreSQL stores do): it binds a session
to its first user, renews the claim for that user, and lets the claim last at least as long as the
session's messages. The Runtime then refuses a run that names another user's session. A store
without it is trusted to isolate users itself (for example one bound to a single user's thread).

Since 0.2.0b3 a memory store should also implement `SessionDeletion.delete_session(tenant_id,
session_id) -> int` (messages, expired ones included, and the owner; returns the message count):
deleting a conversation calls it for the conversation's session, and falls back to `delete`
without it. `UserDataService` (export and erasure of one user, `container.user_data`) needs a few
more methods on every store it is given, and raises `UserDataUnsupportedError` naming the store and
the method before reading or deleting anything when one is missing: run stores `list_for_user` and
`delete_many`; memory stores `sessions_for_user`, `session_owner` and `delete_session`; event,
approval and tool execution stores `forget_runs` (and `list_for_runs` for the last two); learned
memory and MCP token stores `forget_user` (plus `list_for_user` / `servers_for_user`); workflow
checkpoint stores `list_for_user` and `delete_many`. See `persistence/user_data.py` and the
built-in stores for the exact signatures.

A new PostgreSQL migration also regenerates the schema catalog `check_schema` compares against:
`POSTGRES_DSN=... python scripts/schema_catalog.py --update` (tests/unit/test_schema_migrations.py
fails until it names the new migration).

Workflow checkpoint stores additionally require detached reads, tenant isolation, compare-and-set
versions, manifest fingerprints, recoverable-state filtering, and corruption failure behavior. Run
the shared in-memory/PostgreSQL conformance suite when adding another backend. Parent/child workflow
recovery must reconcile the recorded child checkpoint; it must never dispatch a replacement child.

Artifact stores additionally preserve optional run association, content checksum and length,
scan/quarantine status, metadata-only reads, expiry visibility, bounded purge, and permanent
tenant-scoped deletion. Keep large-object SDKs optional and never return another tenant's descriptor
or bytes. Since 0.2.0b4 an artifact has an owner (`user_id` on `Artifact` and `ArtifactDescriptor`,
migration 023): store and return it, and add the optional
`list_for_user(tenant_id, user_id, *, run_id=None, status=None, limit=100)`, which the routes and
user erasure use (without it `list_user_artifacts` filters the tenant listing). Access checks use
`artifact_owned_by` and `ARTIFACTS_ADMIN_SCOPE` from `algen_agent_runtime.artifacts`
(docs/artifacts.md).

For S3-compatible implementations, keep authoritative metadata in a tenant-scoped durable store,
hash tenant identifiers in object keys, request service-side checksums and encryption, conditionally
create objects, verify downloads locally, compensate partial uploads, and make deletion/expiry safe
to retry. Credentials come from workload identity or the SDK chain, not Runtime YAML. Scanner
adapters return only `ArtifactScanResult`; lifecycle state and audit writes belong to
`ArtifactLifecycleService`.

## Usage meter

A deployment's billing or quota system plugs in as a `UsageMeter` (`models/metering.py`) passed to
`build_container(usage_meter=...)`; the `ModelRouter` calls it around every provider dispatch.
Implement `reserve(*, max_input_tokens, max_output_tokens, call: MeterCall)`, `settle(ticket,
usage)` and `release(ticket)`; charge the account named by `call` (tenant, or an id the host put in
`RunRequest.metadata`), keep `call` in the ticket when settling depends on the model, and refuse
only by raising `MeterRefusedError`. A meter whose `reserve` has no `call` parameter is refused with
`ConfigurationError` when the router is built.

Since 0.2.0b3 a streamed call that stops after output reached the caller (Stop, a closed client, a
provider error mid-stream) is settled with an estimate, not released: the request's input estimate
and about four characters per output token streamed. Declare `settle(self, ticket, usage, *,
estimated: bool = False)` to tell estimates apart; a meter without the keyword gets a plain
`settle`. Only a call that delivered nothing is released.

## MCP OAuth token storage

`MCPTokenStore` keeps MCP OAuth tokens keyed by tenant, user and server; `SecretCipher` encrypts
them at rest (`AesGcmCipher`, `encryption` extra). Another backend preserves tenant-scoped keys,
ciphertext bound to its row key, single-use flow state, and never logs or returns token values.

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
- An adapter's `capabilities` must be true of it, and `tests/unit/test_framework_adapter_conformance.py`
  checks them with fakes: `cancellation` stops `invoke` and `stream` (result `CANCELLED`),
  `human_in_the_loop` reports a pause as `AWAITING_INPUT` with `metadata["interrupts"] =
  [{"id", "value"}]`, `persistence` keeps tenants and sessions apart. Since 0.2.0b4 an exception
  becomes a `FAILED` result with `metadata["error"] = {"type", "message"}`, and the adapter sets
  identity itself: LangGraph's thread is `"<tenant_id>:<session_id>"`, OpenAI Agents' context
  identity and `conversation_id` cannot be overridden from `request.context` (a server
  conversation comes only from the constructor's `conversation_id` mapping). AutoGen takes a
  per-request `factory=` (a shared instance is reset and serialised); CrewAI advertises
  `cancellation` only with native `akickoff` (`kickoff_async` runs in a thread) and `streaming=False`.
- LangGraph and OpenAI Agents SDK adapters are supported; AutoGen (frozen, archived upstream) and
  CrewAI (mock crews only) are experimental, and since 0.2.0b5 they carry
  `@experimental(...)` from `algen_agent_runtime.stability`. A new adapter starts experimental
  (decorated, its docstring starting `Experimental:`) until it is tested against the real SDK.
- Telemetry integrations must remain optional and must not include content by default.
- Since 0.2.0b5 Runtime tracers record an exception leaving a span themselves, masked (the same
  `exception` event and `ERROR` status), with any tracer an `ObservabilityAdapter.tracer()`
  returns; an adapter with its own `span_scope` must mask what it records the same way
  (`record_exception` and `telemetry_text` in `observability/safe_spans.py`). `tests/telemetry` (golden files) fails on
  an unannounced change in what any sink records.
- A new Runtime metric goes through `instrument()` / `record()` in
  `algen_agent_runtime.observability.metrics` and gets a `MetricSpec` in `RUNTIME_METRICS`, with
  low-cardinality attributes only (names, statuses, outcomes; never a tenant, user, run or
  conversation id, never content). `tests/unit/test_runtime_metrics.py` checks the catalogue
  against docs/operations.md ("Metrics reference") and the shipped dashboards and alert rules.
