# Framework adapters

Algen Agent Runtime can integrate agents implemented with another orchestration framework. Adapters normalize
invocation, streaming, cancellation, usage, pause state, and metadata without replacing the external
framework's execution model.

| Framework | Invocation | Streaming | Cancellation | Pause state | Multi-agent |
|---|---:|---:|---:|---:|---:|
| LangGraph | Yes | Yes | Task cancellation | Interrupt metadata | Graph-defined |
| OpenAI Agents SDK | Yes | Token and semantic events | Task cancellation | SDK run state | Yes |
| AutoGen AgentChat | Yes | Messages and token chunks | Task cancellation | Team-defined | Yes |
| CrewAI | Yes | Completion lifecycle | Task cancellation | No generic mapping | Yes |

The capability object is deliberately honest: CrewAI does not expose a portable token stream through
this boundary, so its `stream()` produces lifecycle events rather than pretending completion text is
a token stream.

## Installation

```bash
pip install 'algen-agent-runtime[langgraph]'
pip install 'algen-agent-runtime[openai-agents]'
pip install 'algen-agent-runtime[autogen]'
pip install 'algen-agent-runtime[crewai]'
```

`algen-agent-runtime[frameworks]` installs all four. Core imports work when none are installed.

## Usage

Construct and validate the external agent in trusted application code, then register its adapter:

```python
from algen_agent_runtime.frameworks import FrameworkAdapterRegistry, FrameworkRunRequest, LangGraphAdapter

registry = FrameworkAdapterRegistry()
registry.register(LangGraphAdapter(compiled_graph))
result = await registry.get("langgraph").invoke(
    FrameworkRunRequest(
        input="Investigate the failed order",
        run_id="run-123",
        session_id="session-456",
        tenant_id="tenant-a",
        user_id="user-7",
    )
)
```

Use `input_factory` and `output_selector` for application-specific LangGraph state. The default input
uses a `messages` list and the session ID becomes `configurable.thread_id`. OpenAI Agents SDK accepts
an SDK `Agent` plus an optional `Runner` and `RunConfig`. AutoGen accepts an agent or team implementing
`run` and `run_stream`. CrewAI accepts a `Crew`; its default template variable is `input`.

## Security boundary

Adapters never import an agent, graph, or crew by a dotted path from YAML. Loading untrusted Python is
arbitrary code execution. Deployment code constructs trusted objects and explicitly registers them.
Algen Agent Runtime propagates run, session, tenant, and user identifiers, but an external framework remains
responsible for honoring them inside its own persistence and tools.

These are orchestration-framework adapters, not model-provider adapters. Native Algen Agent Runtime agents use
the built-in state machine when uniform policy checkpoints and durable state semantics are required.
