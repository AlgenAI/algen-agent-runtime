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

Run a credential-free paging example with:

```bash
python -m examples.context_offloading
```

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
