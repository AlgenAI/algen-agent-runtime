# Framework adapters

Algen Agent Runtime integrates agents and workflows implemented in external orchestration frameworks. Adapters normalize invocation, streaming, cancellation, token usage, pause state, and telemetry identifiers without replacing the external framework's execution model.

## Honest Capability & Governance Matrix

Algen Agent Runtime explicitly delineates the governance boundaries between Runtime-managed execution and foreign framework execution:

| Framework | Invocation | Trace / Span Propagation | Runtime Governance of Outer Call | Governance of Internal Model / Tool Calls | Durable Pause & Resume |
|---|:---:|:---:|:---:|:---:|:---:|
| **LangGraph** | Supported | Propagates `run_id`, `tenant_id`, `user_id`, `thread_id` | Enforced (timeout, tenancy, cancellation) | Foreign (LangGraph-owned nodes/tools) | Interrupt metadata |
| **OpenAI Agents SDK** | Supported | Propagates `run_id`, `tenant_id`, `user_id` | Enforced (timeout, tenancy, cancellation) | Foreign (OpenAI client/tool-owned) | SDK run state |
| **AutoGen AgentChat** | Supported | Propagates `run_id`, `tenant_id`, `user_id` | Enforced (timeout, tenancy, cancellation) | Foreign (AutoGen team-owned) | Team-defined |
| **CrewAI** | Supported | Propagates `run_id`, `tenant_id`, `user_id` | Enforced (timeout, tenancy, cancellation) | Foreign (Crew task/tool-owned) | Not supported |

> [!IMPORTANT]
> **Governance Boundary**: Algen Agent Runtime governs the outer adapter boundary (request normalization, tenant isolation, request deadlines, and cancellation). Internal model calls and tool calls initiated inside the foreign framework are managed by that framework's native engine. To govern internal calls, configure framework-native hooks or observe them using shared tracing (e.g. Traccia).

## Installation

Framework dependencies are optional and kept strictly lazy:

```bash
pip install 'algen-agent-runtime[langgraph]'
pip install 'algen-agent-runtime[openai-agents]'
pip install 'algen-agent-runtime[autogen]'
pip install 'algen-agent-runtime[crewai]'
```

Install all four with:
```bash
pip install 'algen-agent-runtime[frameworks]'
```

Core Runtime modules remain fully functional when none of these are installed.

## Trusted Application Registration

To prevent arbitrary code execution vulnerabilities, **Runtime never imports external framework code or graphs from YAML dotted path strings**. All external framework objects must be constructed in trusted application code and registered explicitly.

### Option 1: Pass to `build_container`

```python
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.frameworks import LangGraphAdapter

# Compiled LangGraph graph constructed in application code
adapter = LangGraphAdapter(compiled_graph)

container = build_container(settings, framework_adapters=(adapter,))
```

### Option 2: Register directly with `container.frameworks`

```python
from algen_agent_runtime.frameworks import LangGraphAdapter, FrameworkRunRequest

container.frameworks.register(LangGraphAdapter(compiled_graph))

# Invoke through runtime registry
result = await container.frameworks.get("langgraph").invoke(
    FrameworkRunRequest(
        input="Investigate failed order",
        run_id="run-123",
        session_id="session-456",
        tenant_id="tenant-a",
        user_id="user-7",
    )
)
```

## Adapter Details

- **LangGraph**: Uses `input_factory` (defaults to `{"messages": [{"role": "user", "content": request.input}]}`) and `output_selector`. The `session_id` is mapped to `configurable.thread_id` and metadata contains `run_id`, `tenant_id`, and `user_id`.
- **OpenAI Agents SDK**: Accepts an SDK `Agent` plus an optional `Runner` and `RunConfig`.
- **AutoGen**: Accepts an agent or team implementing `run` and `run_stream`.
- **CrewAI**: Accepts a `Crew`; default input mapping is `{"input": request.input}`.

For consuming external tools across standard language-agnostic boundaries (rather than embedding external agent graphs), see the [Model Context Protocol (MCP) Client guide](mcp.md).

## Hook Providers and Framework Adapters

When integrating external framework graphs within Algen workflows, register framework adapters during host initialization or inside a secure workflow hook provider. When loading hook providers dynamically from YAML manifests, use `WorkflowHookLoader` with explicit module allowlists to ensure foreign framework modules are loaded only from trusted packages.
