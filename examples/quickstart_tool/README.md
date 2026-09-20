# Typed capability quickstart

Demonstrates a Runtime workflow resource backed by a deterministic, read-only inventory handler:

```text
lookup (handler/tool resource) -> present (agent)
```

Run with `python -m examples.quickstart_tool.app`, or import this folder into Studio. No credentials or
external services are required. The inventory is synthetic; replace only the domain handler when
adapting the pattern.
