# Multi-agent workflows

Algen Agent Runtime owns the portable workflow contract and executor used by applications that need
more than one agent. A `WorkflowManifest` declares a validated DAG; application code registers named
hooks for domain-specific payload construction, validation, schemas, and deterministic work.

Runtime—not the application or Studio—owns dependency scheduling, concurrency, conditional nodes,
bounded agent repairs, dynamic map-agent fan-out, clarification limits, run lineage, timeouts, failure
propagation, and workflow lifecycle events.

## Manifest nodes

- `agent` invokes one configured Runtime agent.
- `map_agent` invokes one configured Runtime agent for each item at `map_from`, bounded by
  `max_fan_out` and the workflow concurrency limit.
- `handler` invokes a registered deterministic application hook. Use handlers for governed database
  access, analytical methods, or other domain services; do not put executable code in YAML.
- `predicate` evaluates one allowlisted condition and writes a Boolean result.
- `join` combines available dependency outputs using `concat`, `json_array`, or `first`. A join can
  converge mutually exclusive branches; skipped branch outputs are omitted.

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
resume conversational workflows by invoking the same manifest again with accumulated answers;
Runtime prevents an unbounded clarification loop.

## Registering domain hooks

```python
from algen_agent_runtime.workflows import MultiAgentWorkflowExecutor, WorkflowHookRegistry

hooks = WorkflowHookRegistry()
hooks.register_schema("orders.route_schema", RouteDecision.model_json_schema())
hooks.register_builder("orders.route_input", build_route_input)
hooks.register_validator("orders.route_validator", validate_route)
hooks.register_handler("orders.reserve_inventory", reserve_inventory)

state = await MultiAgentWorkflowExecutor(runtime_client, hooks).run(
    settings.workflows["order-routing"],
    {"request": request},
    tenant_id=tenant_id,
    user_id=user_id,
    conversation_id=conversation_id,
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

The executor emits `workflow.started`, node started/repairing/completed/skipped/failed events,
`workflow.input.required`, and one terminal workflow event. Payloads contain workflow and node
identity rather than prompts, model reasoning, credentials, or unrestricted outputs. The returned
`WorkflowExecutionState` contains node attempts, child Runtime run IDs, timestamps, outputs, pause
state, and terminal error information.

The executor is currently an embedded library API. Durable application-specific workflow state and
cross-process resume remain the responsibility of the embedding service until Runtime adds a durable
workflow-state store. Agent runs invoked by the executor still use the configured Runtime stores.

## Runnable examples and Studio import

Runtime ships the example package in both its source and wheel distributions. Studio can import any
example folder, `agent.yaml`, or `config/agent.yaml`; it reads the same `WorkflowManifest` and loads
the declared `hook_provider` from the installed package or the local source path.

- `quickstart_tool` — handler resource followed by an agent.
- `quickstart_approval` and `pattern_approval_workflow` — bounded human checkpoints.
- `pattern_multi_agent_fanout` — validated plan, dynamic map-agent fan-out, join, and synthesis.
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
