# Studio and Runtime alignment audit

Owner: AlgenAI Maintainers

Status: Action required

Last reviewed: 2026-09-24

This audit compares **Algen Agent Studio** (`algen-agent-studio`) with **Algen Agent Runtime** (`algen-agent-runtime`) at Runtime commit `d2cda51` on the `fix/0.1.0a3-stabilization` branch. The Studio baseline is its current `main` working tree, including the uncommitted workflow, checkpoint-store, and secret-vault work present during this review.

The purpose of this document is to identify work that belongs in Studio. It is not a claim that every Runtime extension point needs a visual editor. Where Studio intentionally exposes only a subset, it must preserve unsupported fields losslessly, label the limitation, and fail safely rather than silently changing deployment behavior.

## Corrections to the previous guide

The previous version contained several stale or incorrect claims. They should not be carried into implementation work:

- Runtime does **not** currently implement a Gemini provider, a `deployment_name` provider field, Bedrock, or Vertex endpoints. The provider construction code currently supports `mock`, `openai`, `azure_openai`, `anthropic`, `deepseek`, `mistral`, `ollama`, `huggingface_inference`, `local_transformers`, and `openai_compatible`.
- Runtime's safe starter pack is `starter.calculator`, `starter.json_query`, `starter.file_read`, `starter.directory_list`, and `starter.clock`. It does not contain the previously documented web-search, web-fetch, shell, or write-file tools.
- `WorkflowHookLoader` accepts `allowed_modules` and exposes `load()` / `aload()`. It does not accept `trusted_roots`, modify `sys.path`, or provide lifecycle teardown for the object returned by a hook factory.
- Studio already composes its server with Runtime's `create_app()`. Consequently the Studio host already inherits Runtime request-body middleware, API admission middleware, and `/health/live` and `/health/ready`. The remaining API gap is in per-project configuration and the generated export service, not the Studio composition root.
- The current Studio workflow runner already uses `MultiAgentWorkflowExecutor`, supports typed workflow inputs, approval and clarification decisions, nested workflows, and durable root and parent/child pause/resume, including fail-closed offline approval expiry.

## Executive assessment

The remaining mismatches are concentrated in persistence-service regression coverage. Studio carries the top-level `api` and `mcp_servers` fields through its project schema, importer, runners, and export settings; its generated server uses Runtime's API application. MCP discovery is an explicit operator action and its catalog/status results flow back to Studio.

Provider authoring has a separate correctness issue: Runtime treats keys under `providers` as arbitrary provider **instance IDs**, while Studio rejects most keys that differ from the adapter type and disables creation of a second built-in instance. This breaks valid Runtime configurations such as `primary`, `fallback`, and multiple deployments of the same adapter.

Workflow persistence remains deliberately split between Studio's workflow-history store and Runtime's workflow checkpoint store. The durable recovery coordinator reconstructs the exact snapshotted Runtime configuration and resumes PostgreSQL-backed root approval, clarification, and nested child checkpoints after process loss without redispatching the child.

## Implementation progress

Completed in the current Studio working tree:

- Runtime compatibility dependencies use the `>=0.1.0a3,<0.2` range.
- Standalone exports include deterministic `export-metadata.json` provenance with the project
  revision, Studio version, exact Runtime validation version, and generated bounded Runtime
  requirement; the README reports the same Runtime provenance.
- A dedicated compatibility job installs the audited Runtime `0.1.0a3` commit and validates
  Studio's default project, the representative full governed-data settings, and every discovered
  Runtime `agent.yaml` example through both Runtime and Studio import contracts.
- Studio app and CLI startup now enforce the supported Runtime range before creating workspace
  state, with an actionable installed-versus-required version error.
- Provider instance IDs are no longer constrained to adapter type names, and the UI can add more than one instance of an adapter.
- Provider capability controls are tri-state (inherit/enabled/disabled), and provider serialization preserves Runtime's omitted-field inheritance semantics through save, fork, and export.
- The provider editor exposes Runtime API version, organization, timeout, concurrency, request-rate, and input/output cost controls; all survive export.
- A Studio contract test now builds every catalog provider through Runtime `build_container()` without network access; the catalog and validation set must match, and Gemini is absent until Runtime supports it.
- The Runtime configuration page now exposes deployment API authentication, JWT references, CORS, fail-closed admission defaults, tenant concurrency, and JSON route overrides. New Studio projects export rate limiting enabled by default.
- Generated services expose Runtime's canonical `/v1/runs` endpoint without a Studio-owned legacy
  `/run` facade.
- Export integration coverage now loads the generated server and verifies Runtime malformed/oversized/chunked body handling, structured 413s, 429 retry headers, and readiness degradation.
- Studio hook loading now requires an already-installed module authorized by host-owned `workflow_hook_allowed_modules`; Runtime's loader authorizes before import, returns audit metadata, and preserves factory `aclose()` teardown. Project-local source-path and `sys.modules` manipulation have been removed.
- Hook-backed project exports now fail before an archive is produced, with the hook references and the trusted-host deployment alternative in the error. This prevents Studio from presenting an unpackageable hook workflow as standalone-runnable while approved packaging and asset support remain open.
- Project `api` and `mcp_servers` settings round-trip through import, execution, and export. Export dependencies include Runtime MCP, S3 object-storage, and workflow-checkpoint PostgreSQL extras when configured.
- Exported HTTP services delegate to Runtime `create_app()`; container and ECS readiness checks target `/health/ready`.
- The Runtime configuration page has an MCP editor for stdio, Streamable HTTP, and SSE server contracts, including secret-reference fields, policy overrides, and a visible stdio execution warning.
- MCP discovery is explicitly user-triggered, runs through Runtime's MCP manager, and merges discovered tools into Studio's authoring catalog. Required failures are reported as failed; optional failures are degraded, with no credential values returned.
- The Runtime storage editor now covers every store category, PostgreSQL pool/schema controls, and the complete S3 addressing/encryption contract. Export dependency selection scans all current backend-bearing manifest sections, and deployments support health-gated bundled stores, Runtime-owned empty-database migration, plus credential-free S3 IAM guidance.
- Studio's Runtime editor now uses a scalable five-area control center (models and credentials,
  agent capabilities, data and observability, delivery and security, and advanced extensions)
  instead of rendering the complete Runtime contract as one continuous form. Adapter capability
  overrides and backend-specific fields are progressively disclosed only when relevant, and
  contextual help explains unfamiliar controls without permanently occupying form space.
- Single-agent Studio runs now carry an explicit agent selection through the HTTP request and
  `ProjectRunner`; the Run page shows the selected agent's provider, model, and step limit instead
  of silently executing the first manifest agent. The legacy omitted-agent request remains valid
  and selects the first agent for existing integrations.
- Workflow actions now fail closed in the UI when project changes are unsaved or required
  environment references are unresolved. The disabled action is paired with a visible readiness
  explanation and a direct route to Runtime credentials rather than leaving users to infer why a
  run cannot start.

The persistence-integration item is now implemented: container CI exercises settings emitted by an
export that bundles PostgreSQL and Redis, runs a real model/tool loop, and independently verifies
the PostgreSQL run/tool relationship and Redis-backed conversation memory. Longer-term unchecked
items in the sections below remain follow-up work rather than regressions in this implementation
sequence.

## Priority 0: correctness, security, and deployability

### 1. Establish a Runtime 0.1.0a3 compatibility floor and contract gate

**Evidence**

- Studio previously declared `algen-agent-runtime[traccia]>=0.1.0a1,<0.2`, and the same `a1` floor was used by its optional extras. The current working tree has raised that floor to `0.1.0a3`.
- Studio now references contracts added or changed after `a1`, including workflow checkpoint storage and current workflow semantics.
- Exported `pyproject.toml` files now use the bounded `>=0.1.0a3,<0.2` Runtime line with all selected
  extras. The exact installed Runtime version used during export is recorded separately rather than
  falsely implying that every install in the compatible range is byte-for-byte identical.

**Required Studio work**

1. [x] Raise every Studio Runtime dependency floor to `>=0.1.0a3,<0.2` once `a3` is published.
2. [x] Write the Runtime version used to validate/export the project into export metadata and generated documentation. Exports contain deterministic `export-metadata.json` provenance and repeat it in the README.
3. [x] Generate a bounded Runtime requirement (or an exact version for reproducible bundles), including the selected extras. Studio emits `>=0.1.0a3,<0.2` with its manifest-derived extras and records that complete requirement in the provenance file.
4. [x] Add a compatibility test that loads Studio's default project, every importable Runtime example, and representative full settings through the minimum supported Runtime. The dedicated CI job pins the audited `0.1.0a3` commit, asserts its version, dynamically discovers every Runtime `agent.yaml`, and fails on ignored top-level fields.
5. [x] Make Studio startup fail with a clear version error if an older incompatible Runtime is installed. The shared startup guard enforces `>=0.1.0a3,<0.2`, identifies the installed version, provides an upgrade command, and runs before workspace creation for both app and CLI startup.

**Acceptance criteria**

- A clean environment cannot install Studio with Runtime `0.1.0a1` or `0.1.0a2`.
- An exported archive installs the same compatible Runtime line and reports the version in its README or manifest.

### 2. Fix provider instance semantics and capability narrowing

**Evidence**

- Runtime uses the YAML key under `providers` as a stable registration ID and allows multiple instances of the same adapter type.
- Studio previously emitted `PROVIDER_ID_MISMATCH` for built-in providers whose keys differed from their types and disabled most duplicate adapters. Both restrictions have been removed.
- Runtime 0.1.0a3 preserves adapter capabilities for omitted fields in a partial `capabilities` override. Studio now models capabilities and offers explicit inherit/enabled/disabled controls; omission survives save, fork, and export rather than becoming a default all-false override.
- Studio's provider catalog is hard-coded and has already drifted from Runtime documentation in the previous guide.

**Required Studio work**

1. [x] Remove `PROVIDER_ID_MISMATCH`; validate only that instance IDs are unique and referenced by agents correctly.
2. [x] Let users add multiple instances of any supported adapter. A dedicated instance-ID rename control remains desirable.
3. [x] Add `ModelCapabilities` to frontend types and provide tri-state authoring per capability: inherit (omit), explicitly enabled, or explicitly disabled.
4. [x] Do not serialize a default all-false capability object. Omitted capability fields remain omitted through Studio save, fork, and export so Runtime can inherit adapter discovery.
5. [x] Expose fields already present in Runtime's provider contract (`api_version`, `organization`, timeouts, concurrency, rate and cost data) in an advanced editor and preserve them in exports.
6. [x] Test the Studio catalog against the provider types actually handled by `build_container()`. Gemini remains absent until Runtime has a real adapter and contract tests.

**Acceptance criteria**

- A project with `primary` and `fallback`, both of type `openai`, validates, runs, round-trips, and exports without key rewriting.
- `capabilities: {tools: false}` disables only tools; omitted capabilities retain adapter-reported values.

### 3. Preserve and expose Runtime `api` settings

**Evidence**

- `AppSettings` contains `api`, including authentication, CORS, route quotas, and `max_concurrent_run_requests_per_tenant`.
- Studio has an `api` field, preserves it during import, passes it to Runtime containers, exports it, and exposes deployment-oriented API controls in the visual UI.
- Runtime uses only `max_concurrent_run_requests_per_tenant`; the misleading
  `max_concurrent_runs_per_tenant` configuration key, constructor argument, and property have been
  removed rather than retained as pre-release compatibility shims.
- Studio project defaults and exports explicitly enable fail-closed admission rate limiting rather
  than relying on Runtime's disabled library default.

**Required Studio work**

1. [x] Add Runtime `ApiSettings` to the Studio project schema and frontend contract.
2. [x] Preserve `api` during local, Git, content, example, fork, and archive import paths.
3. [x] Pass it into every `AppSettings` construction and write it to `runtime_settings.yaml`.
4. [x] Author only `max_concurrent_run_requests_per_tenant`. Runtime and Studio reject the removed `max_concurrent_runs_per_tenant` key instead of silently rewriting it, and Runtime no longer exposes the matching constructor argument or property alias.
5. [x] Add deployment-oriented controls for auth mode, JWT secret reference, CORS, default quotas, and route overrides; unsafe development authentication is visibly restricted to loopback unless explicitly acknowledged for a trusted development network.

**Acceptance criteria**

- Import/export round-trip retains all `ApiSettings` fields.
- New exported network services have an explicit admission policy rather than silently relying on disabled defaults.

### 4. Replace the generated minimal server with Runtime's hardened API surface

**Evidence**

- Studio's own application correctly calls Runtime `create_app()`.
- Studio previously generated a bare `FastAPI` app with only `/health` and `/run`. It now generates Runtime's application, preserving Runtime middleware, stable APIs, and lifecycle behavior without retaining the obsolete route.
- Docker and ECS readiness checks now target `/health/ready`.

**Required Studio work**

1. [x] Generate the service with `algen_agent_runtime.api.app.create_app(settings, container)` without replacing its middleware and lifespan.
2. [x] Target `/health/ready` in Docker and ECS templates. Add explicit liveness/readiness configuration to Compose and App Runner next.
3. [x] Remove the Studio-owned `/run` compatibility facade; generated services expose only Runtime's canonical authenticated run API.
4. [x] Add export tests for malformed `Content-Length`, chunked over-limit bodies, structured HTTP 413, HTTP 429 with `Retry-After`, and readiness failure.

**Acceptance criteria**

- Exported deployments expose and pass Runtime's end-to-end API tests rather than a separate, weaker service contract.

### 5. Implement declarative MCP end to end

**Evidence**

- Runtime 0.1.0a3 adds `AppSettings.mcp_servers` with `stdio`, `streamable_http`, and legacy `sse`, secret header references, safe remote egress, tool policy overrides, paginated discovery, required/optional startup behavior, and lifecycle cleanup.
- Studio has MCP project/frontend contracts, import/export and runner wiring, dependency selection, a configuration editor, and an explicit Runtime-backed discovery action.
- Before discovery, validation permits only configured MCP prefixes; after discovery, the UI merges Runtime-returned tools into its catalog. Runtime remains the final startup authority for required servers and actual tool availability.

**Required Studio work**

1. [x] Add Runtime's discriminated `MCPServerConfig` tuple to the project schema and every import/export/run construction path.
2. [x] Add an editor for the server contract. JSON-object fields retain arbitrary headers, environment, secret headers, and policy maps without silently flattening them.
3. [x] Store secret-bearing MCP fields as Runtime-validated `env://` references; Studio never stores literal credentials in the manifest.
4. [x] Require an explicit trust warning for `stdio`, because it launches unsandboxed operator code on the Studio/deployment host.
5. [x] Remote transport guidance is linked to the existing Runtime `SecuritySettings` host/private-network controls.
6. [x] Add the `mcp` extra to exported dependency selection when MCP is configured.
7. [x] Add an explicit discovery action that combines built-in tools with Runtime-discovered MCP tools. Before discovery, allow only configured MCP prefixes; Runtime startup remains authoritative. Optional-server failures are degraded and required-server failures prevent a run at Runtime startup.
8. [x] Display per-server discovery status without exposing resolved secret values. Discovery is never performed automatically, avoiding accidental remote connections or stdio execution when merely viewing a project.

**Acceptance criteria**

- One fixture for each transport round-trips and exports.
- Discovery is an operator action, uses Runtime transport/security/secret resolution, and returns only safe catalog metadata plus ready/degraded/failed status.
- A missing `env://` secret fails before the optional MCP SDK import, matching Runtime behavior.
- Operator policy overrides win over trusted annotations, and untrusted annotations never loosen defaults.

### 6. Adopt `WorkflowHookLoader` without losing lifecycle safety

**Evidence**

- Studio's `workflow_hooks.py` delegates to Runtime's allowlisted loader and no longer mutates global import state. Hook modules must be installed in the Studio host environment and be authorized by host-owned configuration.
- Runtime's loader authorizes exact module segments before import, validates factory/API shape, and returns audit metadata.
- Runtime's loader expects modules to be importable already and does not retain or close a factory result's `aclose()` method. Studio currently supports that teardown behavior.
- `SecuritySettings.trusted_plugin_prefixes` exists, but Studio does not use an explicit module allowlist when loading workflow hooks.

**Required Studio work**

1. [x] Require project hook code to be installed in the Studio host environment; remove per-request `sys.modules`/`sys.path` surgery. Isolated environment provisioning remains a deployment concern.
2. [x] Instantiate `WorkflowHookLoader(allowed_modules=...)` from host-owned `workflow_hook_allowed_modules`, not a project-controlled manifest. Exact package prefixes are authorized before import.
3. [x] Call `await loader.aload(reference, container=..., manifest=..., environment=..., tenant_id=..., user_id=..., project_id=...)` and retain its audit metadata with the loaded hook set.
4. [x] Preserve teardown: Runtime `LoadedHookProvider` now retains an `aclose()` callback and Studio's wrapper invokes it during its existing reverse-order cleanup.
5. [x] Add denial tests proving similarly prefixed modules are rejected before import.

**Acceptance criteria**

- No hook-loading path changes global `sys.modules` membership.
- Unauthorized modules are never imported, authorized async factories work, and all acquired resources close on success and failure.

### 7. Make hook-backed and asset-backed exports self-contained

**Evidence**

- Studio simulation loads only installed hook modules covered by the host-owned allowlist; project imports no longer modify `sys.path` or `sys.modules` to locate hook code.
- Studio blocks hook-backed export by default. It packages a hook only when the module is host-authorized and its Python source is explicitly declared beneath the separately configured export asset root. Exported workflow bootstrap uses Runtime's loader, registry, executor, and teardown lifecycle.
- Runtime 0.1.0a3 adds deterministic answer fixtures and examples whose behavior depends on application-owned Python code.

**Required Studio work**

1. [x] Resolve every `hook_provider` at export time and either package an explicitly trusted module plus required assets, declare an installable dependency, or block export with an actionable error. Studio blocks by default and identifies the hook reference in its HTTP 400 response. A packaging path is available only when the host allowlist authorizes the module and its source plus dependencies are explicitly declared under the approved export root.
2. [x] Generate the corresponding hook module allowlist in trusted host bootstrap code; do not turn manifest references into automatic trust. Studio now exposes a read-only per-project bootstrap report with required modules, configured host prefixes, authorization coverage, and an exact-module operator-review candidate. Project validation marks missing authorization non-executable. The report never imports modules or mutates the host-owned allowlist.
3. [x] Copy only declared assets under an approved project root, with traversal/symlink checks and size limits. `extensions.studio.export_assets` accepts normalized relative file paths, while the host separately configures `ALGEN_AGENT_STUDIO_EXPORT_ASSET_ROOT` plus per-file and aggregate limits. ZIP exports and deployment build contexts reject missing roots/files, absolute or upward paths, symlinks, directories, generated-file collisions, and oversize content; undeclared files and import provenance are ignored.
4. [x] Add an export smoke test for a hook-backed workflow in a clean environment where the original source checkout is unavailable. The test packages an explicitly declared and host-authorized handler module, extracts the archive, removes the original source tree, and executes the workflow through the generated Runtime bootstrap. Additional API coverage proves the HTTP export path embeds both the hook source and exact generated allowlist.
5. [x] Treat framework objects, credentials, open sockets, and other non-serializable process state as host integration requirements, not exportable manifest data. Studio now validates every open-ended extension bag as JSON-shaped declarative data before generation, rejects live objects with field-path diagnostics, and accepts extension credentials only as `env://` or `secret://` references without rendering rejected values. Framework adapter factories remain blocked from standalone export. Bundle metadata and generated documentation record the host-integration contract explicitly.

**Acceptance criteria**

- Every archive that Studio labels runnable is actually runnable after extraction on a clean machine, or export fails before producing the archive.

### 8. Recover paused workflows after Studio restart

**Evidence**

- The current runner persists Studio workflow history and can select Runtime's PostgreSQL workflow checkpoint store.
- In-process approval/clarification messages use `_workflow_resume_queues`; when that queue is absent after restart, the recovery coordinator reconstructs the exact snapshotted Runtime context and addresses the durable checkpoint directly.
- PostgreSQL integration now proves this reconstruction across an actual forced process boundary for root approval and clarification workflows.

**Required Studio work**

1. [x] Persist the Runtime workflow execution ID and manifest fingerprint explicitly alongside the Studio run snapshot. Studio uses Runtime's checkpoint fingerprint algorithm and persists both values through its workflow-history stores.
2. [x] On first recovery access, reconstruct the exact manifest, trusted hooks, container, and `MultiAgentWorkflowExecutor`; load the checkpoint and verify tenant, version, fingerprint, and parent/child lineage. Studio reconstructs the Runtime container, exact reachable workflow registry, authorized hooks, and executor from its JSON-safe settings snapshot.
3. [x] Reattach approval/clarification endpoints to a restart-safe coordinator rather than a process-local queue. When no local queue exists, Studio rebuilds the verified durable context, recovers the Runtime checkpoint, applies the decision/clarification, persists the resulting state, and closes the temporary resources.
4. [x] Mark memory-backed paused runs as non-recoverable after restart and present a clear terminal/retry state instead of leaving them indefinitely paused. A resume attempt without its process-local coordinator transitions the paused run and step to `failed` with a clear restart/new-run message.
5. [x] Cover parent/child workflows, expired approvals, conflict retries, and already-completed nodes (which must not replay). Forced-process PostgreSQL coverage proves root and parent/child persistence plus offline expiry; unit regressions retain context cleanup, no-replay rejection, and conflict preservation.

**Acceptance criteria**

- A PostgreSQL-backed workflow can pause, lose the Studio process, restart, accept a decision, and complete without replaying completed nodes.

## Priority 1: feature completeness and operational parity

### 9. Add the real starter-tool pack as an explicit Studio feature

Runtime starter tools are deliberately disabled by default and are registered programmatically through `additional_tools=get_starter_tools(...)`; they are not an `AppSettings` field. Studio should therefore use a Studio-owned declarative selection (for example `extensions.studio.tool_packs`) and translate it at runtime/export bootstrap rather than writing an unknown Runtime YAML key.

Required work:

- [x] Show the five actual tools and their Runtime definitions in the tool catalog. They are visibly unavailable until Studio registers the pack, so catalog visibility cannot be mistaken for container registration; regression coverage confirms the pack remains absent by default.
- [x] Allow per-agent enablement only when `extensions.studio.tool_packs` selects `starter`; Studio validation rejects enabled starter tools otherwise.
- [x] Require an explicit relative `extensions.studio.starter_workspace_root`; host execution resolves it beneath the managed project workspace and exports resolve it beneath the extracted bundle.
- [x] Pass the identical starter-tool set to Studio runs, evaluation runs, workflow runs, recovery, smoke tests, CLI exports, and server exports through Runtime's `additional_tools` contract.
- [x] Add tests proving the pack remains absent by default and workspace roots cannot traverse or escape through symlinks.

### 10. Complete storage and deployment dependency selection

**Evidence**

- Backend Studio schemas import Runtime's full `StorageSettings`; the frontend contract and editor now cover every Runtime store, S3 encryption/addressing configuration, PostgreSQL pool sizes, schema initialization, and retention.
- `runtime_dependency()` scans `workflow_store` and selects `object-storage` for S3 artifacts, with regression coverage for both paths.
- Dependency selection now accounts for storage, cache, MCP, JWT authentication, analytical/distributed/query-source PostgreSQL use, every non-memory retrieval backend, Traccia telemetry, and local transformers. Framework bootstrap extras remain part of the explicit adapter work in section 11.
- Generated Compose defaults to external stateful services. Studio can explicitly select bundled PostgreSQL and/or Redis through `extensions.studio.export_services`; generated services are internal-only, persistent, health checked, and gate agent startup with `service_healthy` conditions.
- Bundled PostgreSQL exports require `storage.initialize_schema=true` and rely exclusively on Runtime's packaged migration runner. Studio container CI creates a fresh database, starts the exported Runtime settings, and verifies the migration ledger plus run/workflow tables.
- S3 exports include a prefix-scoped IAM policy template and workload-role guidance for ECS, App Runner, KMS, local profiles, and S3-compatible services. AWS credential fields are rejected rather than serialized.

Required work:

1. [x] Bring frontend storage types and controls to parity with Runtime, including S3 encryption/addressing fields and pool sizes.
2. [x] Include `workflow_store` in PostgreSQL extra detection and `artifact_store: s3` in `object-storage` detection.
3. [x] Derive extras from the whole manifest, not only a subset of storage fields. All current backend-bearing Runtime sections are covered; future framework registrations are tracked in section 11.
4. [x] Generate optional PostgreSQL and Redis services with health checks and `depends_on` conditions when requested; external-service mode remains the default and is authorable in the Runtime storage UI.
5. [x] Use Runtime's packaged migrations/initialization path and test an empty-database startup. Exports contain no Studio migration copy; container CI verifies Runtime migrations against a uniquely created database.
6. [x] For S3 exports, document IAM requirements and pass only secret references; never embed AWS credentials. Generated archives use boto3's credential chain and contain a scoped policy template, not access keys.

### 11. Represent framework adapters honestly, including OpenAI Agents SDK

**Evidence**

- Runtime 0.1.0a3 promotes the OpenAI Agents SDK adapter to a supported surface but explicitly does not provide durable resume and does not auto-dispatch normal `AgentRuntime` agents through it.
- Studio now detects the OpenAI Agents SDK through its real `agents` import. Capability responses and the inspector separate Runtime-supported adapters (LangGraph and OpenAI Agents SDK) from experimental adapters (AutoGen and CrewAI), while retaining the installed-ID summary for older clients.
- Studio shows installed frameworks but has no trusted registration/bootstrap or export-extra path for them.

Required work:

1. [x] Detect the OpenAI Agents SDK using the `agents` import name and report supported versus experimental adapters separately.
2. [x] Label framework availability as an application-code extension, not a provider or ordinary Studio agent toggle. Capability entries carry an explicit integration kind and trusted-registration requirement, and both relevant Studio surfaces explain the distinction.
3. [x] Add an explicit trusted bootstrap/hook pattern for constructing and registering adapters, with the appropriate Runtime extra (`openai-agents`, `langgraph`, `autogen`, or `crewai`). Studio authorizes installed factory modules through a host-owned allowlist before import, registers adapters for runs/evaluations/workflows/recovery, selects the matching dependency extra, and gives Runtime lifecycle ownership. Standalone exports block these host integrations rather than converting project references into trust.
4. [x] Preserve the governance warning: Runtime governs the outer adapter call, not framework-internal model/tool calls. Studio now exposes this as machine-readable adapter capability metadata, shows it in adapter configuration and inspection, emits a non-blocking validation warning for configured adapters, and retains it in trusted-host export diagnostics and documentation.
5. [x] Do not offer durable resume for OpenAI Agents SDK interruptions; its Runtime capability is `persistence=False`. Studio now derives adapter persistence from Runtime's capability objects, publishes it through the capability API, labels adapters in configuration and inspection, and warns that OpenAI Agents SDK interruption metadata is not restart-resumable.

### 12. Expose or deliberately preserve advanced Runtime settings

Studio's backend schema and exporter already preserve conversation presentation/follow-ups, analytical execution, query governance/sources, distributed execution, and feature flags, but the frontend project type omits most of them and offers little or no authoring UI.

Studio should choose one of two explicit strategies for every Runtime section:

- provide a typed editor and validation; or
- provide a lossless advanced YAML/JSON editor, mark the section as not visually editable, and include round-trip tests.

Silent omission from TypeScript types is not sufficient. At minimum add frontend types for `conversation_presentation`, `conversation_followups`, `analytical_execution`, `query_governance`, `distributed_execution`, and `feature_flags`, and test that an unrelated visual edit does not erase any of them.

Required work:

1. [x] Add frontend contracts matching Runtime's conversation presentation/follow-ups, analytical execution, query governance, distributed execution, and feature flag settings.
2. [x] Give every Runtime section an authoring strategy. Providers, agents, workflows, retrieval/query sources, storage, cache, telemetry, security, API admission, and MCP retain dedicated visual controls. Runtime lifecycle and the six advanced sections use a lossless JSON editor explicitly marked as not otherwise visually editable.
3. [x] Validate the advanced editor's section envelope and boolean feature flags locally, while leaving authoritative field, range, and cross-section validation to Runtime on save.
4. [x] Add frontend and persisted-project round-trip tests proving advanced settings and unrelated application-owned data survive advanced edits and unrelated visual edits.

### 13. Improve workflow-template authoring for Runtime 0.1.0a3 validation

Studio already receives Runtime's new manifest-time rejection of unknown or nested placeholders because it uses `WorkflowManifest`.

Required work:

1. [x] Present valid built-in and declared initial-input tokens plus transitive upstream outputs in both `{{key}}` and `{{key.output}}` form in a node-scoped placeholder picker.
2. [x] Offer `{{item}}` only for `map_agent` nodes and state the restriction beside other node templates.
3. [x] Add a non-persisting preflight endpoint that invokes Runtime's `WorkflowManifest` validator and maps errors to the exact `/nodes/{index}/input_template` field before save.
4. [x] Add regression tests for qualified and direct output keys, declared input-schema properties, unknown and nested keys, map-item restrictions, and unmatched placeholder delimiters. Runtime now rejects malformed delimiter leakage at manifest validation as well as unresolved complete placeholders at render time.

This is a UX and preflight gap, not a need to duplicate Runtime's validator.

### 14. Normalize structured HTTP errors in the Studio frontend

Runtime now returns stable body-limit and admission error codes. Studio's frontend fetch helpers generally collapse non-success responses into a message and do not consistently retain `code`, `limit_bytes`, or `Retry-After`.

Required work:

1. [x] Add a shared structured HTTP error and fetch path that parses `REQUEST_PAYLOAD_TOO_LARGE`, `INVALID_CONTENT_LENGTH`, `RATE_LIMIT_EXCEEDED`, and `CONCURRENCY_LIMIT_EXCEEDED` while retaining status, `limit_bytes`, and response detail.
2. [x] Preserve `Retry-After` and show a visible countdown before one bounded replay of safe `GET`/`HEAD` requests; cap the client wait at 30 seconds without discarding the server's original value.
3. [x] Never automatically replay `POST`, `PUT`, `PATCH`, or `DELETE` mutations. Their structured error remains available to the caller for an explicit user action.
4. [x] Classify and label quota errors as Studio-host or deployed-Runtime responses from the addressed URL.
5. [x] Add frontend regression tests for stable error fields, scope labeling, capped safe retries, single-replay bounds, and mutation non-replay.

## Priority 2: drift prevention

### 15. Generate parity reports from Runtime contracts

Hard-coded provider, tool, storage, and feature catalogs drift easily.

Required work:

1. [x] Compare `AppSettings.model_fields` with Studio's project model, Runtime-config import fields, and generated `runtime_settings.yaml` keys.
2. [x] Extract Runtime provider-construction branches and compare them with Studio's provider catalog.
3. [x] Compare `StorageSettings` and `ProviderSettings` JSON Schema properties, plus every Runtime storage enum, with frontend representations.
4. [x] Exercise generated dependency selection for PostgreSQL, Redis, S3, MCP, JWT auth, every retrieval backend, Traccia, local transformers, and every framework adapter; reject extras not declared by the installed Runtime distribution. This audit also closed the masked S3/PostgreSQL dependency combination.
5. [x] Compare Runtime workflow node kinds and fields with frontend types and the workflow-form policy.
6. [x] Compare stable Runtime request-limit/admission error codes with the frontend structured-error union.
7. [x] Run the machine-readable report in CI, fail on unexplained drift, upload the JSON artifact, and regression-test that a removed frontend field is detected.

The audit maintains an explicit allowlist for losslessly preserved workflow fields that do not yet have dedicated controls. The report records `Studio workflow UX` as owner and the preservation rationale; new fields fail the audit until typed and assigned a policy.

### 16. Validate the complete typed workflow-input contract

Runtime accepts JSON Schema Draft 2020-12 for workflow inputs, while Studio previously checked only
that required top-level fields were non-empty. That allowed invalid numeric, string, nested-object,
array, composition, and additional-property values to reach Runtime before the operator saw an error.

Required work:

1. [x] Validate Studio input values with JSON Schema Draft 2020-12 semantics before enabling workflow execution. Format annotations remain informational, matching Runtime's validator without a format checker.
2. [x] Cover references and definitions, composition and conditionals, constants and enums, object/property constraints, arrays and tuple items, numeric bounds, and string patterns/lengths.
3. [x] Render path-specific inline messages using Runtime's `workflow input validation failed at <path>` convention, including missing and additional property names.
4. [x] Keep generated controls for ordinary top-level scalar, enum, array, and object properties while providing a complete JSON-payload editor for nested or advanced schemas.
5. [x] Treat malformed JSON drafts as invalid input and keep the Run action disabled until both the draft and schema validate.
6. [x] Expand the frontend Runtime JSON Schema type so supported Draft 2020-12 keywords are explicit while unknown annotation/extension keywords remain losslessly representable.
7. [x] Add regression coverage for required/minimum constraints, nested array paths, `$ref`/`$defs`, `oneOf`, patterns, bounds, uniqueness, and forbidden additional properties.

Studio continues to require an object at the workflow-input root because its request and persisted
run contracts intentionally store `inputs` as an object. Runtime remains authoritative at execution,
so a browser-side check does not weaken or replace server validation.

### 17. Prove durable root-workflow recovery against PostgreSQL

Unit coverage previously exercised the recovery coordinator with fake executors. It did not prove
that Studio history, immutable execution snapshots, Runtime checkpoints, and migrations remain
coherent across a real process loss.

Required work:

1. [x] Start Studio workflow history and Runtime workflow checkpoints against the same fresh PostgreSQL database, with both schemas initialized through their production paths.
2. [x] Pause one first-class approval workflow and one clarification workflow, then forcibly terminate the owning worker process without graceful cancellation.
3. [x] Construct a fresh `ProjectRunner` and PostgreSQL history store, recover from the persisted immutable snapshot, and apply the approval and clarification without a process-local queue.
4. [x] Verify both recovered Studio runs and steps are terminal, their pause payloads are cleared, and a second fresh history-store instance reads the committed terminal states.
5. [x] Query Runtime's checkpoint table independently and verify both checkpoint rows are terminal rather than merely trusting Studio's returned models.
6. [x] Run the test in the existing container-integration job, which installs the required `postgres` and `redis` extras and provides PostgreSQL 16.

The test uses a forced subprocess boundary because graceful `ProjectRunner.close_all()` correctly
records active workflows as cancelled and therefore cannot model a host crash.

### 18. Prove durable parent/child recovery without redispatch

A nested pause is represented by two Runtime checkpoints and one Studio step. Recovery must preserve
that split: the Studio endpoint addresses the parent, Runtime delegates into the original child, and
the parent consumes the child's canonical output after it completes.

Required work:

1. [x] Add a parent workflow that dispatches an exact-version child containing a first-class approval checkpoint, with both manifests retained in the immutable execution snapshot.
2. [x] Persist the parent's child-workflow pause and original child checkpoint ID before forcibly terminating the worker process.
3. [x] Resume through a fresh Studio runner and verify Runtime recovers the parent registry, delegates the decision into the child, and completes both checkpoints.
4. [x] Verify the parent node retains exactly one `child_workflow_id` equal to the pre-crash ID and that only two checkpoints exist for the nested root, proving recovery did not redispatch a replacement child.
5. [x] Verify the child retains its parent workflow ID, parent node ID, root ID, depth, and complete workflow ancestry after recovery.
6. [x] Reopen Studio's PostgreSQL history store and independently query Runtime's checkpoint table to verify terminal persistence on both sides.

This coverage runs in the same PostgreSQL container-integration job as root approval/clarification
recovery and empty-database migration.

### 19. Prove offline approval expiry fails closed

An approval can expire while no Studio process is attached. The first late decision request must
recover Runtime's checkpoint, observe the expiry, persist the terminal failure, and decline to apply
the supplied decision or reviewer metadata.

Required work:

1. [x] Persist a one-second approval checkpoint in PostgreSQL, forcibly terminate Studio, and wait against the checkpoint's exact `expires_at` timestamp rather than relying on an arbitrary sleep.
2. [x] Submit a late approval through a fresh `ProjectRunner` and verify Runtime expires the checkpoint during recovery before `decide_approval()` can apply the decision.
3. [x] Synchronize Runtime node output, error, skipped reason, pause state, workflow output/error, and terminal timestamp back into Studio history during recovered execution.
4. [x] Synchronize every Runtime node after terminal recovery so descendants skipped by an expired approval do not remain falsely queued in Studio.
5. [x] Emit terminal step/workflow events without emitting `workflow.resumed` when recovery itself discovers the terminal state.
6. [x] Reopen Studio's PostgreSQL history and query Runtime's checkpoint independently to verify both retain `workflow approval expired`, no approval output exists, and the late reviewer decision was not recorded.
7. [x] Add a focused unit regression proving the recovery coordinator never invokes `decide_approval()` for an already-expired checkpoint.

### 20. Prove generated PostgreSQL/Redis wiring and parent-before-tool persistence

Compose shape tests alone did not prove that the emitted Runtime settings selected the live
adapters, or that a PostgreSQL tool ledger entry could satisfy its foreign key to the parent run
during an actual Runtime tool loop.

Required work:

1. [x] Generate an export with explicitly bundled PostgreSQL and Redis, then verify the agent is
   health-gated on both services and receives the generated internal service URLs.
2. [x] Load the generated `runtime_settings.yaml` without hand-building substitute settings and
   exercise it against the PostgreSQL 16 and Redis 7 services already supplied by container CI.
3. [x] Configure the generated settings for PostgreSQL run and tool-execution stores plus Redis
   memory, start the Runtime container, and execute a real mock-model → registered-tool → model
   completion loop.
4. [x] Query PostgreSQL independently and require an inner join between the completed run and its
   completed tool ledger row. The ledger table's foreign key to `algen_agent_runtime_runs(id)` makes
   this a regression proof that the parent is persisted before the tool reservation is inserted.
5. [x] Read the completed conversation through Runtime's Redis memory store, independently ping
   Redis, and verify the unique session key exists before cleaning it up.
6. [x] Keep this coverage in the existing container-integration job with the
   `.[dev,postgres,redis]` installation, rather than weakening optional dependency guards in normal
   Studio installations.

The local suite skips this test when service URLs are absent. CI supplies both URLs and runs the
test against real services.

## Items already aligned in the current Studio working tree

These should be retained and regression-tested rather than reimplemented:

| Runtime capability | Current Studio state | Remaining caveat |
| --- | --- | --- |
| Runtime-owned workflow manifest | Studio uses `WorkflowManifest` directly, migrated the old split workflow field, and provides Runtime-backed template preflight | Retain generated node-field drift tests |
| Typed workflow inputs | Draft 2020-12 validation, path-specific messages, generated controls, full-payload JSON editing, and the `inputs` request path are present | Retain advanced-schema and malformed-draft regressions |
| Approval and clarification UI | Runtime decisions/comments are wired to active executors and the durable recovery coordinator; forced-process PostgreSQL coverage proves root, nested, and fail-closed offline-expiry behavior | Retain recovery, lineage, expiry, and conflict regressions |
| Nested workflows and snapshots | Runner reconstructs the reachable registry from immutable snapshots; forced-process PostgreSQL coverage proves original-child recovery and lineage without redispatch | Retain nested lineage and no-redispatch regressions |
| Workflow checkpoint backend selection | `workflow_store` is authorable, exported, included in dependency selection, and initialized against an empty PostgreSQL database in container integration | Retain empty-database migration coverage |
| Runtime middleware on Studio host and exports | Studio and generated project servers use Runtime `create_app()` | Retain Runtime API parity tests |
| Request byte settings | Studio stores/edits the limits and exported Runtime apps enforce them | Retain malformed/chunked body regression tests |
| Secret storage | Current Studio work adds an encrypted credential vault while manifests retain references | MCP and all new secret fields must use the same reference-only path |
| Runtime workflow validation | Pydantic parsing enforces topology, schemas, and 0.1.0a3 template rules; Studio displays inline template preflight feedback | Retain unknown-token and malformed-delimiter regressions |
| PostgreSQL/Redis Runtime fixes | Studio consumes Runtime stores and UUID-like run IDs; generated settings are exercised against live PostgreSQL/Redis and a real persisted tool loop | Retain generated-service, foreign-key ordering, and Redis-memory integration coverage |

## Delivery order

Recommended sequencing minimizes rework:

1. Compatibility floor and contract-parity CI.
2. Provider instance/capability semantics.
3. Add `api` and `mcp_servers` to the lossless project model and all construction paths.
4. Secure hook loading and self-contained export packaging.
5. Generate deployments from Runtime `create_app()` and fix dependency/service selection.
6. Implement restart-safe workflow recovery.
7. Add starter tools, framework bootstrap, and advanced configuration UX.
8. Finish structured frontend errors and inline workflow validation.

## Definition of alignment

Studio is aligned when a Runtime-valid configuration within Studio's declared support policy can be imported, inspected, edited without unrelated data loss, simulated with the same registrations and security boundaries, exported, and run in a clean environment with equivalent behavior. Fields or extension points outside that policy must be preserved or rejected explicitly; they must never be silently ignored, rewritten, trusted, or downgraded.
