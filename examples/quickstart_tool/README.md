# quickstart_tool

**Tier:** Quickstart | **Status:** 🚧 Placeholder — not yet implemented

> [!NOTE]
> This example will introduce one typed read-only tool before showing write-side-effect tools.

## Planned scope

- One agent with one typed read-only tool (e.g. a lookup or calculator).
- Demonstrates `ToolDefinition` with Pydantic-derived schema, explicit permissions, `SideEffect.READ`, and `Idempotency.IDEMPOTENT`.
- Runnable in under five minutes.
- One smoke test without paid credentials.

## Implementation checklist

- [ ] Create `agent.yaml`.
- [ ] Create `app.py` registering and invoking the tool.
- [ ] Create `manifest.yaml`.
- [ ] Create `tests/test_smoke.py`.
- [ ] Update this README with setup/run/test/reset instructions and expected output.
