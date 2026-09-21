# Multi-agent workflows

Algen Agent Runtime owns the portable workflow contract and executor used by applications that need
more than one agent. A `WorkflowManifest` declares a validated DAG; application code registers named
hooks for domain-specific payload construction, validation, schemas, and deterministic work.

Runtime—not the application or Studio—owns dependency scheduling, concurrency, conditional nodes,
bounded agent repairs, dynamic map-agent fan-out, clarification limits, run lineage, timeouts, failure
propagation, first-class human approval checkpoints, and workflow lifecycle events.

## Typed workflow inputs and outputs

A manifest may declare JSON Schema Draft 2020-12 contracts with `input_schema`, `output_schema`, and
`output_key`. Runtime validates each schema when the manifest is loaded, validates caller input before
any node starts, and validates the canonical output before completing the workflow.

Typed input is stored at the reserved `WorkflowExecutionState.values["inputs"]` key. Domain hooks and
templates can read nested values such as `inputs.application_id`. Other state keys remain available
for clarification history and node outputs without weakening the caller contract.

```yaml
workflows:
  candidate-review:
    name: candidate-review
    version: 1.0.0
    input_schema:
      type: object
      required: [application_id, job_id]
      additionalProperties: false
      properties:
        application_id: {type: string, minLength: 1}
        job_id: {type: string, minLength: 1}
    output_schema:
      type: object
      required: [recommendation]
      properties:
        recommendation: {enum: [advance, manual_review, decline]}
    output_key: decision
    nodes: [...]
```

```python
state = await executor.run(
    manifest,
    {"inputs": {"application_id": "app-123", "job_id": "job-456"}},
    tenant_id=tenant_id,
    user_id=user_id,
)
```

Manifests without `input_schema` retain the legacy free-text convention and remain compatible.
`output_schema` requires an `output_key` produced by a declared node. A missing or invalid typed input
or output fails the workflow closed with a path-specific configuration error. JSON Schema describes
data semantics; hosts such as Studio may render it, but do not own or translate it.

## Manifest nodes

- `agent` invokes one configured Runtime agent.
- `map_agent` invokes one configured Runtime agent for each item at `map_from`, bounded by
  `max_fan_out` and the workflow concurrency limit.
- `handler` invokes a registered deterministic application hook. Use handlers for governed database
  access, analytical methods, or other domain services; do not put executable code in YAML.
- `predicate` evaluates one allowlisted condition and writes a Boolean result.
- `join` combines available dependency outputs using `concat`, `json_array`, or `first`. A join can
  converge mutually exclusive branches; skipped branch outputs are omitted.
- `approval` pauses the durable workflow for an authenticated human decision. It can approve,
  approve schema-valid modified parameters, or reject a proposed action without invoking an agent.
- `workflow` dispatches an exact child workflow name and version through a `WorkflowRegistry`, stores
  it as an independent checkpoint, and returns its canonical output to the parent node.

Every node declares an `output_key`. Downstream hooks read prior results from
`WorkflowHookContext.state.values`. `run_if` uses the Runtime's allowlisted predicates. Agent nodes can
reference an inline JSON schema or a registered `output_schema_hook`, plus a validator and
`max_repairs`. A repair receives `invalid_output` and `validation_error` in its hook context and is
linked to the preceding attempt through `parent_run_id`.
Agent nodes can use a registered `input_builder` or a declarative `input_template`, and may declare a
bounded `max_iterations` plus `loop_until`. Hosts such as Studio can supply `agent_runner` to retain
their own live-event and history integration without replacing Runtime scheduling semantics.

Nodes may also declare `resources`: secret-free references to Runtime tools, query sources,
retrieval indexes, memory, caches, stores, telemetry, or application services. A reference records
the resource kind, configured name, access mode, and optional description. It documents the
architecture for hosts such as Studio without embedding credentials or changing execution
semantics; application hooks remain responsible for domain-specific resource use.

Clarification is a pause rule on a node. It declares the predicate, question path, safe state paths,
the collection that records prior clarification answers, and `maximum_occurrences`. Applications
resume the same checkpoint with `executor.resume(...)` and the accumulated answers. Runtime reruns
only the paused node, preserves completed predecessors, prevents an unbounded clarification loop,
and excludes time spent waiting for human input from the workflow execution deadline.

Approval is a dedicated node rather than a clarification convention. It declares a human-readable
`prompt`, optional safe `review_from` paths, an optional object at `parameters_from`, an optional
Draft 2020-12 `parameters_schema`, whether modification is allowed, an expiry of 1 second to 7 days,
and a rejection policy. Runtime checkpoints the pause before emitting
`workflow.approval.required`. An accepted decision produces a durable result containing
`decision`, validated `parameters`, `decided_by`, optional `comment`, and `decided_at` at the node's
`output_key`.

Modification defaults off and rejection defaults to `skip_dependents`; both require an explicit
manifest choice to relax.

```yaml
- id: approve-change
  kind: approval
  depends_on: [propose-change]
  output_key: approval
  approval:
    prompt: Review this change before it is applied.
    review_from: [proposal.summary]
    parameters_from: proposal.parameters
    parameters_schema:
      type: object
      required: [record_id, enabled]
      additionalProperties: false
      properties:
        record_id: {type: string, minLength: 1}
        enabled: {type: boolean}
    allow_modification: true
    rejection_policy: skip_dependents
    expires_seconds: 1800
```

`skip_dependents` is the safe rejection default: descendants do not execute. Choose `continue`
only when the downstream branch explicitly consumes a rejection as a valid business outcome. Joins
may converge rejected and skipped branches. Expired approvals fail closed when a decision or
recovery is attempted; deployment-owned maintenance must actively recover paused workflows if
expiry must be enforced without user or startup traffic.

Approval review values and parameters are persisted in the workflow checkpoint and exposed to an
authorized host. Declare only deliberately reviewable, secret-free state paths. Never put
credentials, presigned URLs, raw private artifacts, or unrestricted model context in them. Runtime
lifecycle events include the prompt, expiry, decision, and reviewer identity, but not review values,
parameters, or comments.

Each node also has an explicit `recovery_policy`. The default, `fail`, refuses to replay a node that
was running when a process stopped. Set `retry` only for an idempotent or externally reconcilable
node. Read-only model calls and deterministic calculations are common candidates; email, payment,
write, and other externally visible handlers must remain fail-closed unless their adapter provides a
stable idempotency key and reconciliation contract.

## Parent/child workflow composition

Use a `workflow` node when one durable business process needs a bounded, independently inspectable
subprocess. The node requires `workflow_name`, `workflow_version`, and an `input_builder` or legacy
`input_template`. For a typed child, the builder returns the object validated by the child's
`input_schema`; Runtime stores it under the child's `inputs` key. The child should declare
`output_key` so its canonical result becomes the parent node's output.

```yaml
- id: screen-application
  kind: workflow
  workflow_name: candidate-screening
  workflow_version: 2.0.0
  input_builder: campaign.application_input
  output_key: screening
  recovery_policy: fail
```

Register every trusted reachable manifest with its own hooks:

```python
from algen_agent_runtime.workflows import WorkflowRegistry

registry = WorkflowRegistry()
registry.register(parent_manifest, parent_hooks)
registry.register(child_manifest, child_hooks)
executor = MultiAgentWorkflowExecutor(
    runtime_client,
    parent_hooks,
    store=container.workflow_checkpoints,
    workflow_registry=registry,
)
```

Every child receives a distinct workflow ID plus `parent_workflow_id`,
`parent_workflow_node_id`, `root_workflow_id`, shared `correlation_id`, depth, and versioned ancestry.
Runtime rejects unregistered versions, dispatch cycles, and nesting beyond the configured maximum.
Child clarification and approval pauses propagate to the parent checkpoint and resume the recorded
child—not a new dispatch. During recovery, a parent reconciles its last recorded child checkpoint;
it does not create another child identity. Runtime checkpoints the generated child ID and validated
input before dispatch, so a crash before child-checkpoint creation starts that same recorded child on
recovery. The parent manifest's `maximum_concurrency` bounds whole child executions; each child then
applies its own concurrency and fan-out limits internally.

## Registering domain hooks

```python
from algen_agent_runtime.workflows import MultiAgentWorkflowExecutor, WorkflowHookRegistry

hooks = WorkflowHookRegistry()
hooks.register_schema("orders.route_schema", RouteDecision.model_json_schema())
hooks.register_builder("orders.route_input", build_route_input)
hooks.register_validator("orders.route_validator", validate_route)
hooks.register_handler("orders.reserve_inventory", reserve_inventory)

executor = MultiAgentWorkflowExecutor(runtime_client, hooks, store=container.workflow_checkpoints)
state = await executor.run(
    settings.workflows["order-routing"],
    {"request": request},
    tenant_id=tenant_id,
    user_id=user_id,
    conversation_id=conversation_id,
    emit=publish_workflow_event,
)

if state.status == "awaiting_input":
    state = await executor.resume(
        settings.workflows["order-routing"],
        state.id,
        tenant_id=tenant_id,
        values={"clarifications": [(state.pause["question"], answer)]},
        emit=publish_workflow_event,
    )

if state.status == "awaiting_approval":
    state = await executor.decide_approval(
        settings.workflows["order-routing"],
        state.id,
        tenant_id=tenant_id,
        user_id=authenticated_user_id,
        decision="approved",  # or "modified" / "rejected"
        modified_parameters=None,
        comment="Reviewed against change request CR-123.",
        emit=publish_workflow_event,
    )
```

Individual hook names are registry keys, not imports. Registration fails on duplicate names and execution fails
closed when a referenced hook is missing. Keep hooks focused on domain facts and service calls; do
not recreate scheduling, retry loops, fan-out, or run-correlation logic inside them.

A portable manifest may declare `hook_provider: package.module:create_hooks` so an embedding host can
locate the application package that registers those names. Runtime validates and preserves this
reference but never imports or executes it automatically; the host controls trust, loading,
dependency injection, and cleanup. This keeps the workflow language in Runtime while domain code
remains in the application package.

## Events and state

The executor emits `workflow.started`, node started/repairing/completed/skipped/failed/rejected
events, `workflow.input.required`, `workflow.approval.required`,
`workflow.approval.decided`, `workflow.approval.expired`, and one terminal workflow event. Payloads contain workflow and node
identity rather than prompts, model reasoning, credentials, or unrestricted outputs. The returned
`WorkflowExecutionState` contains node attempts, child Runtime run IDs, timestamps, outputs, pause
state, and terminal error information.

Runtime checkpoints the workflow before every emitted lifecycle event. Configure
`storage.workflow_store: postgres` for cross-process durability; the in-memory default is intended
for local development and tests. Checkpoints are tenant-scoped, optimistically versioned, and bound
to a canonical manifest fingerprint so a host cannot silently resume changed topology under the same
name and version.

On startup, an embedding service can call `container.workflow_checkpoints.list_recoverable()`, load
the matching trusted manifest and hook provider, and call `executor.recover(...)`. Awaiting-input
and unexpired awaiting-approval workflows remain paused; expired approvals fail closed during
recovery. Interrupted nodes retry only when their manifest explicitly declares
`recovery_policy: retry`; all others fail closed. Runtime owns these recovery semantics, while the
host owns trusted hook construction and deciding which application workflows to activate.

## Runnable examples and Studio import

Runtime ships the example package in both its source and wheel distributions. Studio can import any
example folder, `agent.yaml`, or `config/agent.yaml`; it reads the same `WorkflowManifest` and loads
the declared `hook_provider` from the installed package or the local source path.

- `quickstart_tool` — handler resource followed by an agent.
- `quickstart_approval` and `pattern_approval_workflow` — bounded human checkpoints.
- `pattern_multi_agent_fanout` — validated plan, dynamic map-agent fan-out, join, and synthesis.
- `pattern_child_workflow` — version-pinned child dispatch, lineage, canonical output, and recovery.
- `pattern_governed_research` — bounded retrieval, cited synthesis, and verification.
- `pattern_langgraph_governance` — graph execution through the Runtime framework adapter.
- `pattern_evaluation_gate` — deterministic evaluation and promotion evidence.
- `reference_customer_support` — parallel retrieval/entitlement branches and safe action recording.
- `reference_incident_response` — parallel telemetry, remediation checkpoint, and synthetic execution.
- `reference_invoice_exceptions` — document gate, bounded extraction repair, parallel reconciliation,
  policy routing, and a non-paying recommendation.
- `case_study_responsible_hiring` — security gate, parallel specialist review, bias review, and a
  reviewer-owned outcome.

The deterministic workflow examples require no paid credentials and are exercised by
`tests/unit/test_example_workflows.py`. Application hooks contain domain facts and synthetic service
behavior only; the Runtime remains responsible for topology validation and execution semantics.
