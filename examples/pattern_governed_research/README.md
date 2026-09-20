# pattern_governed_research

**Tier:** Pattern | **Status:** ⚠️ Work in progress — not yet runnable

> [!WARNING]
> This example is under active development. The agent configuration previously declared `core.http` as an enabled tool, but that tool is not registered by `build_container`. The configuration has been fixed (unregistered tool references removed). The example will be published once a retrieval/HTTP tool is implemented and registered.

## What this example will demonstrate

- Governed web or document retrieval using a registered `Retriever` or HTTP tool.
- Citation verification: every factual claim cites a retrieved source identifier.
- Guardrail policies enforcing `secrets`, `authorization`, and `side_effects` boundaries.
- ReAct planning with bounded steps and explicit evidence accumulation.

## Implementation checklist

- [ ] Implement a typed retrieval or HTTP tool (e.g. `research.fetch` or `research.search`).
- [ ] Register the tool via `container.tools.register()` in `app.py`.
- [ ] Re-enable `enabled_tools` and `tool_permissions` in `agent.yaml`.
- [ ] Add synthetic fixture data and a golden output for deterministic CI.
- [ ] Add `tests/test_smoke.py` and `tests/test_evaluation.py`.

## Production gaps

- No tool is registered: the agent cannot retrieve evidence yet.
- No evaluation fixtures exist.
- Citation flow is configured but not exercised.
