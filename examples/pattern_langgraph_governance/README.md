# pattern_langgraph_governance

**Tier:** Pattern | **Status:** 🚧 Placeholder — not yet implemented

> [!NOTE]
> This example will demonstrate a LangGraph application wrapped by the Traccia governance adapter, launched through the normal Traccia run API.

## Planned scope

- A LangGraph graph wrapped by a Traccia framework adapter.
- Launched through the standard `AlgenAgentRuntimeClient.run()` API (not a custom LangGraph invocation).
- Demonstrates that governance controls (policy interception, approval, audit, telemetry) apply to framework-backed agents identically to native agents.
- One smoke test that exercises the adapter path without requiring credentials.

## Implementation checklist

- [ ] Implement the Traccia–LangGraph framework adapter.
- [ ] Create `agent.yaml` declaring `framework: langgraph`.
- [ ] Create `app.py` as a thin CLI entry point using the standard run API.
- [ ] Create `manifest.yaml`.
- [ ] Create `tests/test_smoke.py`.
- [ ] Update this README with setup/run/test/reset instructions and expected output.

## Production gaps

- Framework adapter does not exist yet.
- Governance controls (approval, audit, budget) are not yet bridged to LangGraph lifecycle hooks.
