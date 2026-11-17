# LangGraph governance adapter pattern

Shows Runtime's `LangGraphAdapter` normalizing graph input, tenant/user/run identity, status, and
output inside a portable workflow. Its included graph is protocol-compatible and dependency-free;
replace `ExampleGraph` with a compiled LangGraph graph without changing the adapter boundary.

Run `python -m algen_agent_runtime_examples.pattern_langgraph_governance.app` or import `agent.yaml` into Studio. Install
`algen-agent-runtime[langgraph]` only when substituting a real LangGraph implementation.

Since 0.2.0b4 the adapter runs every graph on thread `"<tenant_id>:<session_id>"` (a caller-supplied
`thread_id` or identity is refused), reports a graph stopped by `interrupt()` as `AWAITING_INPUT`
with `metadata["interrupts"]`, and returns `FAILED` instead of raising the graph's exception. See
[framework adapters](../../docs/framework-adapters.md#capabilities-pauses-and-isolation-since-020b4).
