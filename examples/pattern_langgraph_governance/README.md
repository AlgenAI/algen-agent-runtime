# LangGraph governance adapter pattern

Shows Runtime's `LangGraphAdapter` normalizing graph input, tenant/user/run identity, status, and
output inside a portable workflow. Its included graph is protocol-compatible and dependency-free;
replace `ExampleGraph` with a compiled LangGraph graph without changing the adapter boundary.

Run `python -m examples.pattern_langgraph_governance.app` or import `agent.yaml` into Studio. Install
`algen-agent-runtime[langgraph]` only when substituting a real LangGraph implementation.
