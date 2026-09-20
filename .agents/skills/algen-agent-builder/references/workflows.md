# Runtime multi-agent workflows

Use a `WorkflowManifest` under the top-level `workflows` mapping. Choose nodes deliberately:

- `agent`: one bounded agent run;
- `map_agent`: bounded dynamic fan-out over a runtime list;
- `handler`: deterministic domain operation registered by a hook provider;
- `predicate`: declarative branch condition;
- `join`: merge dependency outputs using `concat`, `json_array`, or `first`.

Express edges only with `depends_on`. Use `run_if` for conditional execution, `pause` for bounded
clarification or human input, `max_repairs` plus a validator for structured-output repair,
`max_iterations` plus `loop_until` for bounded loops, and `failure_policy` for explicit failure
semantics. Cap workflow concurrency, timeout, and map fan-out.

Declare tools, query sources, retrieval, memory, storage, cache, telemetry, and services in each
node's `resources`. These references are secret-free operational topology and allow Studio to draw
the real dependency graph.

The hook factory returns `WorkflowHookRegistry`. Register only names referenced by the manifest:
builders, validators, schemas, and deterministic handlers. Runtime owns scheduling, run state,
events, repair, clarification, resume, failure propagation, and cancellation.

Use `examples/pattern_multi_agent_fanout`, `pattern_approval_workflow`,
`pattern_governed_research`, and the reference applications as current examples.

