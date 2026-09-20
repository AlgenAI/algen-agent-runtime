# Multi-agent fan-out pattern

Demonstrates the Runtime-owned topology below:

```text
planner -> map_agent (maximum 3, concurrent) -> join -> editor
```

It also declares retrieval resources, structured schemas, validation, bounded repair, correlation,
and concurrency. Run `python -m examples.pattern_multi_agent_fanout.app` or import `agent.yaml` into
Studio. The evidence and models are deterministic so the complete example runs without credentials.
