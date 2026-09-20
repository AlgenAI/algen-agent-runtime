# quickstart_cloud_chat

**Tier:** Quickstart | **Status:** 🚧 Placeholder — not yet implemented

> [!NOTE]
> This example will be the smallest hosted-provider path: one agent, one cloud API key, one command.

## Planned scope

- Single agent using a hosted provider (OpenAI or Anthropic).
- No tools, no retrieval — pure conversational inference.
- Runnable in under five minutes from a clean install.
- One smoke test that does not require credentials (mock/stub mode).

## Implementation checklist

- [ ] Create `agent.yaml` with a minimal cloud-provider agent definition.
- [ ] Create `app.py` as a thin CLI entry point.
- [ ] Create `manifest.yaml`.
- [ ] Create `tests/test_smoke.py` with a deterministic mock mode.
- [ ] Update this README with prerequisites, setup, run, test, and reset instructions.
