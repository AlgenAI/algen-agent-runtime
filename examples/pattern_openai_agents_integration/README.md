# Pattern: OpenAI Agents SDK Integration

This pattern demonstrates how an application-owned OpenAI Agents SDK `Agent` is registered into Algen Agent Runtime via `OpenAIAgentsAdapter` and consumed through `container.frameworks.get("openai_agents")`.

## Governance and Responsibility Boundaries

- **Runtime Normalization**: Runtime normalizes the outer invocation result (`FrameworkRunResult`), stream lifecycle events (`STARTED`, `DELTA`, `STEP`, `COMPLETED`), and propagates identity metadata (`run_id`, `tenant_id`, `user_id`, `session_id`) into context and tracing headers.
- **SDK Autonomy**: The OpenAI Agents SDK owns its internal execution loop, model routing, function tools, multi-agent handoffs, guardrails, and interruption mechanisms. Runtime native policies and tool registries do not automatically intercept internal SDK calls.
- **Governed tools**: Runtime tools handed to the SDK agent through `GovernedToolBridge` run through the Runtime's tool executor. The example's `incidents.lookup` runs governed, `incidents.close` (which requires approval) is refused there, and the SDK's own `read_runbook` runs without the Runtime. See [governing tool calls inside a framework](../../docs/framework-adapters.md#governing-tool-calls-inside-a-framework).

## Running the Example

It needs the `openai-agents` extra (also in `frameworks`). The SDK agent runs offline on the SDK's
`ScriptedModel`, so no key is needed:

```bash
pip install -e '.[dev,openai-agents]'
python -m examples.pattern_openai_agents_integration.app
```
