# Artifact lifecycle

Runtime owns the artifact contract used by agents, workflows, API hosts, and visual workbenches.
Artifacts may be staged before a run, so `run_id` is optional; tenant identity is always required.
Do not place artifact bytes, credentials, or host-local paths in a workflow manifest.

Each artifact carries immutable content identity (`size_bytes` and SHA-256), media type, a display
name, string metadata, optional expiry, and one of these lifecycle states:

- `pending_scan`: uploaded but not yet cleared by the deployment's malware/content scanner;
- `available`: cleared or produced by a trusted Runtime operation;
- `quarantined`: retained for investigation but unavailable to ordinary consumers.

The HTTP API accepts a bounded raw request body at `POST /v1/artifacts`; `name`, `media_type`,
optional `run_id`, and optional `expires_in_seconds` are query parameters. Optional string metadata
is a JSON object in `X-Artifact-Metadata`. Uploads begin in `pending_scan`. A trusted scanner or
operator changes the state with `ArtifactLifecycleService`; an explicitly authorized operational
override is available at `PATCH /v1/artifacts/{id}/status` and requires `artifacts:scan`. Listing and
metadata endpoints do not load blob bytes. Pending and quarantined blobs cannot be downloaded through
the public API.

```bash
curl -X POST \
  -H 'X-Tenant-ID: acme' \
  -H 'X-User-ID: operator' \
  -H 'X-Scopes: artifacts:read artifacts:write' \
  -H 'X-Artifact-Metadata: {"purpose":"candidate_resume"}' \
  --data-binary @candidate.pdf \
  'http://localhost:8000/v1/artifacts?name=candidate.pdf&media_type=application%2Fpdf'
```

Use an artifact ID in typed workflow inputs instead of base64 data. JSON Schema can identify these
fields with `format: artifact` and constrain the accepted type with `contentMediaType`; Runtime
treats those annotations as portable UI/host hints while the application hook resolves and validates
the referenced artifact within the same tenant.

## Retention and storage

`expires_at` makes an artifact inaccessible as soon as it expires. Operators should schedule
`ArtifactStore.purge_expired()` to reclaim bytes; the operation is bounded by a caller-provided
limit. Explicit deletion is tenant-scoped and permanent.

The built-in memory store is for tests and ephemeral development. PostgreSQL can store metadata and
small blob payloads durably. For production object storage, install
`algen-agent-runtime[object-storage]` and select `s3`. PostgreSQL remains the authoritative metadata
and lifecycle store; bytes use an S3-compatible bucket:

```yaml
storage:
  artifact_store: s3
  postgres_dsn: env://ALGEN_AGENT_RUNTIME_POSTGRES_DSN
  artifact_s3_bucket: algen-production-artifacts
  artifact_s3_prefix: algen-agent-runtime/artifacts
  artifact_s3_region: ap-south-1
  artifact_s3_server_side_encryption: aws:kms
  artifact_s3_kms_key_id: alias/algen-runtime-artifacts
```

Credentials are not accepted in Runtime configuration. Boto3 resolves workload identity, instance
roles, web identity, or its standard environment/profile chain. Custom S3-compatible endpoints must
use HTTPS; loopback HTTP is allowed only for local emulators. The adapter sends SHA-256 to the object
service, requires server-side encryption, uses conditional create, verifies downloaded bytes against
PostgreSQL metadata, and compensates by deleting the blob if metadata persistence fails. Object keys
contain a tenant hash rather than the tenant identifier. Presigned URLs are deliberately not exposed
because they would bypass Runtime tenant and lifecycle checks.

## Offloaded tool context

Agents can opt into storing large, policy-processed tool results as artifacts before those results
enter the model transcript. The tool execution record continues to hold its completed result for
idempotent recovery; this feature reduces active model context, not execution-record storage.

Within an agent manifest, enable the reader explicitly:

```yaml
enabled_tools: [reports.fetch, core.context_read]
tool_permissions: [context.read] # Also include any permissions required by reports.fetch.
context_policy:
  offload_tool_results: true
  tool_result_max_inline_bytes: 16384
  tool_result_preview_chars: 256
  artifact_retention_seconds: 86400
```

The default container wires `ContextArtifactService` to its configured `ArtifactStore`. Direct
`AgentRuntime` construction accepts `context_artifacts=ContextArtifactService(artifact_store)`;
the runtime registers `core.context_read` when this service is supplied. The tool name is reserved
in that registry. Offloading fails configuration validation before starting a run if storage,
the enabled reader, or its permission is missing. The policy defaults to disabled.

The source tool first passes output-schema validation, `after_tool` policy evaluation, and its
`max_result_bytes` limit. Runtime then serializes that processed value as JSON. Results exceeding
the inline byte threshold are stored with a checksum and replaced in the tool message by a bounded
JSON reference containing an artifact ID, preview, size, checksum, and read instructions. The
preview is shortened further if escaping or Unicode would exceed the inline threshold. Artifact
write failure fails the run; it does not reexecute a completed source tool or send its oversized
payload to the model. Artifact-store byte limits remain in effect.

`core.context_read` accepts `artifact_id`, a zero-based `offset`, and `limit` from 1 to 4,096 Unicode
characters (default 4,096). It returns `content`, `next_offset` (null at EOF), and total character
count. Concatenate pages to recover the stored JSON, then parse it if needed. Offsets describe the
stored JSON text, not array rows or parsed fields. Request smaller pages for small model windows.
Read results are not offloaded again. Read-back passes through the ordinary tool permissions,
policies, execution records, and audit events. Storage adapters currently load the bounded artifact
blob to serve a page; this is bounded model output, not an object-store range-read API.

Readers enforce tenant, originating run, originating user, purpose, expiry, and `available` status.
They cannot read arbitrary uploads or another run's artifacts, including sibling subagent runs.
No cross-call result cache is used. Completed read replays also recheck metadata authorization,
lifecycle, and checksum through `Tool.replay_validator`; other tools retain their existing replay
behavior unless they supply this optional validator. Validators receive the recorded `ToolResult`
and current `ToolContext` and must raise to deny replay.

Runtime-produced context artifacts are marked `available`; this does not assert that tool text is
trusted instructions or has undergone malware scanning. References and pages label content as
untrusted data. Repeated offloads of the same processed result within one tool execution reuse a
stable ID. Retention is measured from run start and is not renewed by replay, so configure a window
long enough for expected pauses. Standard lifecycle changes and deletion remain effective.
`RunResult.artifacts` includes these references, and `context.offload` audit records contain IDs
and sizes without previews or content. API downloads continue to use the existing artifact scopes.
Context artifact IDs are opaque: new IDs use a `ctx_` prefix and letters to avoid incidental PII
redaction of digit sequences. Existing UUID references remain authorized and reusable.

Run a credential-free paging example with:

```bash
python -m examples.context_offloading
```

## Recoverable history compaction

Built-in context builders retain execution messages in `RunState.context_history`, separately from
the packed model-facing `messages`. Packing never removes retained history. By default, compaction
also retains original messages inline; archive-backed checkpoints can opt out of that duplication.
An opt-in summarization step replaces an older prefix in the active view while leaving recent groups
verbatim. Its source messages are stored in a run-scoped archive readable through `core.context_read`.

```yaml
enabled_tools: [core.context_read] # Include the agent's other tools as needed.
tool_permissions: [context.read]
context_policy:
  compact_history: true
  compaction_trigger_tokens: 12000
  compaction_trigger_ratio: 0.8
  compaction_keep_recent_groups: 6
  compaction_max_input_tokens: 8000
  compaction_max_output_tokens: 1024
  artifact_retention_seconds: 86400
```

The effective trigger is the smaller of `compaction_trigger_tokens` and
`floor(compaction_trigger_ratio * available_message_tokens)`, with a minimum of one token.
The available allowance is the smallest of the built-in builder budget and configured windows of
allowed concrete model profiles, after output, tool/schema, profile-extension, and safety
reservations. Retrieval builders also reserve `max_retrieval_tokens`. Profiles without configured
windows contribute no model limit; profiles with no room for messages are skipped, as in router
fallback. This conservative calculation uses configured profiles, without probing provider health.
The comparison counts assembled history, system instructions, working state, and skill projections.
Set the ratio to `null` to retain the fixed history-only trigger. Compaction remains disabled unless
`compact_history` is true. Later policy transformations are subject to the router's final budget
checks; tuning does not guarantee that every request can fit.

Use tool-result offloading as well for large payloads. Compaction never splits
an assistant tool-call message from its results and waits until pending calls have completed. At
most one bounded prefix is summarized per context-build pass; an indivisible group exceeding the
summary input allowance is left untouched. Summary prefixes are bounded by both
`compaction_max_input_tokens` and configured model allowances with summary-output and safety
reservations; summary requests have no tool/schema reservation. This feature supports the default and retrieval
builders; custom builders keep their existing behavior and cannot enable built-in compaction.

Summary requests include the previous summary and new source messages as untrusted data. They use
the agent's allowed model profiles, normal model policies, router budgets/fallback, and run deadline.
The summary request has no tools. Returned usage and model-call count are charged to the same run
and checkpointed even when summary validation or archive storage subsequently fails. No separate
automatic summary retry is performed beyond router candidate fallback. A truncated, empty, oversized,
tool-calling, or non-reducing summary fails explicitly without advancing the cursor.

The summary remains an assistant message labelled historical data, never a system instruction or
authorization. System messages and the latest user message remain available verbatim. The summary
is protected during packing, so a request whose essential content still cannot fit fails rather
than silently dropping the summary. If summarization is disabled, the active view can still omit
old messages while canonical history retains them.

Archives contain `start`, `through`, `messages`, and `previous_archive_id`. The chain recovers earlier
compaction sources without placing all archive IDs in every prompt. The archive passes the memory-write
policy before storage; policy redactions are permitted but JSON message structure and recovery links
must survive. Cursor, summary, archive reference, and usage are checkpointed before the content-free
`context.compacted` event is published. Events include the effective `trigger_tokens` and retained
history's absolute `history_offset`, without source content. New context reads and read replays use the same access and
lifecycle checks as tool-result archives. Retention and artifact byte limits apply to both.

By default, canonical history uses run-store retention, while archives use artifact retention.
For long runs, enable `context_policy.archive_compacted_history: true` alongside `compact_history`.
After a successful archive write, Runtime removes the compacted prefix from subsequent checkpoints.
`context_history` then contains the retained tail, `context_history_offset` its absolute start,
and `context_history_pinned` the archived system messages and latest user message still needed
verbatim. The summary and archive chain identify older sources. New checkpoints no longer duplicate
those source payloads in `messages`. The archive and checkpoint must succeed before the compaction
event is emitted; a failed write leaves the last durable checkpoint usable for recovery.

Archive-backed history follows **artifact retention**: expiry, deletion, or quarantine can make old
source messages unavailable even while the run and its summary survive. Choose durable artifact
storage and retention long enough for pauses and audit needs. Runtime does not reload expired
sources or bypass read authorization to rebuild history. Recovery uses the checkpoint's protected
messages, summary, and tail; source read-back remains explicit. Disabling archival later retains
future messages inline but does not restore previously removed sources. This bounds historical
payload duplication, not total checkpoint size: protected messages, the uncompacted tail, and artifact
references can still grow, especially if new history arrives faster than one prefix per pass.

The new checkpoint fields default to offset zero and no pinned messages, so existing inline
checkpoints load without a database migration. Older Runtime versions cannot read the new fields;
drain or finish these runs before downgrading. Memory storage remains ephemeral. Old checkpoints
seed history from their surviving active messages and
cannot recover content discarded by earlier Runtime versions. Runtime code and extensions adding
execution messages should use `RunState.append_message()`; direct edits to the active `messages`
list do not append to initialized canonical history. Fresh retrieval evidence is rebuilt from the
retriever, rather than repeatedly copied into canonical execution history.

Run an offline example with `python -m examples.context_compaction`.

## Storage bounds and scanning

`security.max_artifact_bytes` is enforced by the API and every built-in store. Bucket lifecycle
rules should complement—not replace—Runtime expiry cleanup. `purge_expired()` deletes the blob before
its metadata record and is safe to retry when provider deletion is idempotent.

Artifact scanning is deployment-owned. Runtime deliberately does not claim that a MIME type or file
extension proves content safety. Implement `ArtifactScanner.scan()` and invoke it through
`ArtifactLifecycleService.scan()`. Runtime verifies the artifact, requires a terminal `available` or
`quarantined` result, performs a compare-and-set transition from `pending_scan`, and appends scanner,
version, reason code, checksum, and size to the audit log. Scanner failures leave the artifact
pending. Do not use the status override endpoint as the normal scanner integration.
