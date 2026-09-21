# Runtime multi-agent workflows

Use a `WorkflowManifest` under the top-level `workflows` mapping. Choose nodes deliberately:

- `agent`: one bounded agent run;
- `map_agent`: bounded dynamic fan-out over a runtime list;
- `handler`: deterministic domain operation registered by a hook provider;
- `predicate`: declarative branch condition;
- `join`: merge dependency outputs using `concat`, `json_array`, or `first`;
- `approval`: durable approve/modify/reject checkpoint owned by Runtime;
- `workflow`: dispatch one exact, registered child workflow version with typed input and canonical
  output.

Declare `input_schema` when a workflow accepts more than one value or anything more structured than
legacy free text. Use JSON Schema Draft 2020-12, keep the payload secret-free, and read it from
`WorkflowHookContext.state.values["inputs"]`. Declare `output_schema` together with `output_key` when
callers depend on a canonical typed result. Runtime validates both contracts; Studio renders the same
input schema and must not replace it with Studio-specific form metadata.

Represent file, image, or audio inputs as artifact IDs, not base64 or local paths. Use a string schema
with `format: artifact` and, when known, `contentMediaType`. The host stages the file through the
Runtime artifact lifecycle; hooks must resolve it in the current tenant and reject expired,
quarantined, mismatched-media-type, or oversized content.

Do not mark uploads available from a workflow hook. A deployment-owned `ArtifactScanner` must run
through `ArtifactLifecycleService` first; workflows consume only `available` descriptors and should
record artifact IDs—not object keys or presigned URLs—in their typed state.

For typed execution, pass `{"inputs": payload}` as the executor's initial values. Do not add
clarification history, credentials, or host bookkeeping to the typed payload; those belong in
separate workflow state keys.

Express edges only with `depends_on`. Use `run_if` for conditional execution, `pause` for bounded
clarification or human input, `max_repairs` plus a validator for structured-output repair,
`max_iterations` plus `loop_until` for bounded loops, and `failure_policy` for explicit failure
semantics. Choose `recovery_policy` explicitly: keep the fail-closed default for side effects and use
`retry` only when replay is idempotent or externally reconcilable. Cap workflow concurrency, timeout,
and map fan-out.

Use a first-class `approval` node—not a clarification predicate—before consequential work. Declare a
clear prompt, only deliberately reviewable `review_from` paths, optional secret-free parameters and
their JSON Schema, a bounded expiry, and whether modification is permitted. Keep the defaults
`allow_modification: false` and `rejection_policy: skip_dependents`; use `continue` only for a branch
designed to consume a rejection.
Resume it with `executor.decide_approval`, passing the authenticated reviewer identity. Treat
approval parameters and review context as persisted operator-visible data: never include credentials,
presigned URLs, unrestricted model context, or raw private artifacts.

Use `storage.workflow_store: postgres` outside local development. Resume a clarification with
`executor.resume`; never invoke `run` again because that creates a new workflow and replays completed
predecessors. At host startup, list recoverable Runtime checkpoints, reconstruct only trusted hook
providers, and call `executor.recover` with the exact versioned manifest.

For composition, register every trusted child manifest and its hooks in a `WorkflowRegistry`, then
give that registry to the parent executor. A `workflow` node must pin `workflow_name` and
`workflow_version`; never resolve a floating latest version. Runtime owns correlation and lineage,
child checkpoint identity, depth and cycle limits, clarification/approval propagation, recovery, and
canonical output validation. Parent concurrency bounds whole child executions. Runtime checkpoints
the child ID and input before dispatch so recovery either reconciles that child or creates the same
recorded identity after a pre-creation crash. Resume or decide through the parent checkpoint. Do not
invoke the child directly or copy its nodes into the parent. Child inputs and outputs must be
JSON-compatible and secret-free. See `examples/pattern_child_workflow`.

Declare tools, query sources, retrieval, memory, storage, cache, telemetry, and services in each
node's `resources`. These references are secret-free operational topology and allow Studio to draw
the real dependency graph.

The hook factory returns `WorkflowHookRegistry`. Register only names referenced by the manifest:
builders, validators, schemas, and deterministic handlers. Runtime owns scheduling, run state,
events, repair, clarification, resume, failure propagation, and cancellation.

Use `examples/pattern_multi_agent_fanout`, `pattern_approval_workflow`,
`pattern_child_workflow`, `pattern_governed_research`, and the reference applications as current
examples.
