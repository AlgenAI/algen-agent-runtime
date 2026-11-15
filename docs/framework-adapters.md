# Framework adapters

Algen Agent Runtime integrates agents and workflows implemented in external orchestration frameworks. Adapters normalize invocation, streaming, cancellation, token usage, pause state, and telemetry identifiers without replacing the external framework's execution model.

## Support status

Decided for `1.0` on 2026-11-15 ([deployment scope](production-readiness.md#deployment-scope)):

- **Supported:** LangGraph and the OpenAI Agents SDK. Tested against the real SDKs; covered by the
  [deprecation window](api-stability.md#support-and-deprecation-policy-1x).
- **Experimental, frozen:** AutoGen AgentChat (`autogen` extra). AutoGen is archived upstream (its
  README says it is no longer maintained and gets no bug or security fixes, and points to
  Microsoft Agent Framework). The adapter takes fixes only, no new features, and may be removed
  in a minor release. New projects should not start on it.
- **Experimental:** CrewAI (`crewai` extra). Tested with mock crews only; it becomes supported
  once it has real-SDK tests.

Experimental adapters are outside the compatibility promise
([experimental interfaces](api-stability.md#experimental-interfaces)).

## Honest Capability & Governance Matrix

Algen Agent Runtime explicitly delineates the governance boundaries between Runtime-managed execution and foreign framework execution:

| Framework | Invocation | Trace / Span Propagation | Runtime Governance of Outer Call | Governance of Internal Model / Tool Calls | Durable Pause & Resume |
|---|:---:|:---:|:---:|:---:|:---:|
| **LangGraph** | Supported | Propagates `run_id`, `tenant_id`, `user_id`, `session_id`; thread `<tenant_id>:<session_id>` | Enforced (timeout, tenancy, cancellation) | Foreign (LangGraph-owned nodes/tools) | Interrupts detected (`AWAITING_INPUT`); the checkpointer persists the thread; resume through your `input_factory` |
| **OpenAI Agents SDK** | Supported | Propagates `run_id`, `tenant_id`, `user_id`, `session_id` | Enforced (timeout, tenancy, cancellation) | Foreign (OpenAI client/tool-owned) | Tool approvals detected; `RunResult` in `resume_state` (in-process, no durable resume) |
| **AutoGen AgentChat** | Supported | Through your `factory` / `input_factory` only | Enforced (timeout, tenancy, cancellation) | Foreign (AutoGen team-owned) | Handoff to the user detected; no resume, no state across requests |
| **CrewAI** | Supported | Through your `input_factory` only | Enforced (timeout, tenancy; cancellation only with native `akickoff`) | Foreign (Crew task/tool-owned) | Not supported |

> [!IMPORTANT]
> **Governance Boundary**: Algen Agent Runtime governs the outer adapter boundary (request normalization, tenant isolation, request deadlines, and cancellation). Internal model calls and tool calls initiated inside the foreign framework are managed by that framework's native engine. To govern internal calls, configure framework-native hooks or observe them using shared tracing (e.g. Traccia).

## Capabilities, pauses and isolation (since 0.2.0b4)

Each adapter's `capabilities` now say what it actually does, and
`tests/unit/test_framework_adapter_conformance.py` checks every flag against the adapter:

| Adapter | `streaming` | `cancellation` | `persistence` | `human_in_the_loop` | Pause reported as `AWAITING_INPUT` | Isolation between tenants and sessions |
| --- | :---: | :---: | :---: | :---: | --- | --- |
| `LangGraphAdapter` | Yes | Yes | Only when the graph has a checkpointer | Yes | `interrupt()`: the default (v1) `"__interrupt__"` key or the v2 `.interrupts`, in `invoke` and `stream` | Thread `"<tenant_id>:<session_id>"`, always |
| `OpenAIAgentsAdapter` | Yes | Yes | No | Yes | Tool approvals (`result.interruptions`), in `invoke` and `stream`; the `RunResult` is kept in `resume_state` | Identity in `context` set by the adapter; an OpenAI server conversation only from the constructor's `conversation_id` mapping |
| `AutoGenAdapter` | Yes | Yes (with a `CancellationToken` when `autogen_core` is installed) | No | No (a handoff is reported, but cannot be resumed through the adapter yet) | A final `HandoffMessage` to `handoff_target` (default `"user"`) | `factory=` builds a team per request; one shared instance is `reset()` before each run and runs one request at a time |
| `CrewAIAdapter` | No (`stream` emits STARTED and the terminal event) | Only when the crew has native `akickoff` (CrewAI 1.x; its `kickoff_async` runs `kickoff` in a thread that keeps running) | No | No | None | Stateless |

**Pauses.** A paused run returns `status=AWAITING_INPUT` and
`metadata["interrupts"] = [{"id": ..., "value": ...}]`: the interrupt's stable id and its
structured value (LangGraph's `Interrupt.value`; for OpenAI Agents
`{"tool_name", "arguments"}` with the tool call id as `id`; for AutoGen the handoff's source,
target and content). LangGraph's `metadata["interruptions"]` (text) and OpenAI Agents'
`metadata["interruptions"]` (now with `call_id` and `arguments`) are kept for compatibility.
LangGraph also reports `metadata["checkpointer"]`: resuming a thread needs a checkpointer
(`graph.compile(checkpointer=...)`). Without one, LangGraph 1.2 still reports the interrupt, but
the paused state is not saved, so the run cannot be resumed; `capabilities.persistence` is then
False. Resuming through the adapter is not part of 0.2.0b4: resume a
LangGraph thread with your `input_factory` returning `Command(resume=...)` for the same
`session_id`, and an OpenAI Agents run from `result.resume_state.to_state()`.

**LangGraph streaming.** `stream()` asks for `stream_mode=["updates", "values"]`: updates become
`STEP` events, and the result is built from the final state, exactly as `invoke()` builds it
(same `output_selector` input, same usage, same interrupts). Token usage is summed from
`usage_metadata` on the messages in the final state; with a checkpointer, messages already in
the thread before the run are not counted again.

**Identity cannot be overridden.** The adapter sets the run's identity from the request:

- LangGraph: `configurable.thread_id` is always `"<tenant_id>:<session_id>"`, so two tenants
  with the same session id never share a checkpoint. A
  `request.context["langgraph_config"]` that sets another `thread_id`, or metadata naming
  another `run_id`, `tenant_id`, `user_id` or `session_id`, is refused (`FAILED`,
  `PolicyDeniedError`). Choose the thread with `session_id` (for a workflow, the workflow run
  id).
- OpenAI Agents: the run `context` tools read always holds the request's `run_id`,
  `tenant_id`, `user_id` and `session_id`; a `request.context` naming other values is refused.
  `openai_agents_options` may not set `context`, `conversation_id`, `previous_response_id`,
  `session` or `run_config`; pass `run_config` (for example to turn SDK tracing on) to
  `OpenAIAgentsAdapter(..., run_config=...)`. `conversation_id` names an OpenAI
  server-managed conversation, so the adapter sends one only when you pass
  `OpenAIAgentsAdapter(..., conversation_id=lambda request: ...)`, which looks up the
  conversation your host created for that tenant's session (return `None` for none).
- AutoGen and CrewAI receive no identity from the adapter: AutoGen's `factory` and CrewAI's
  `input_factory` receive the request and can hand `tenant_id` and the rest to the agents. The
  adapters add nothing to the framework's inputs.

**Failures and cancellation.** `invoke()` returns `status=FAILED` with
`metadata["error"] = {"type": ..., "message": ...}` (message redacted with `redact_secrets` and
cut to 500 characters) instead of raising the framework's exception, and `status=CANCELLED`
when `cancel(run_id)` stopped it. `stream()` ends with a `FAILED` event whose `result` carries
that status (`failed` or `cancelled`); a completed or paused stream ends with `COMPLETED`.
`cancel()` works for streams too. Cancelling the caller's own task still raises
`asyncio.CancelledError`.

### Upgrade notes for 0.2.0b4

- Code that caught framework exceptions from `invoke()`, or `CancelledError` after `cancel()`,
  now checks `result.status` (`FAILED`, `CANCELLED`) and `result.metadata["error"]`.
- LangGraph threads are `"<tenant_id>:<session_id>"` instead of `session_id`: checkpoints
  written by an earlier version under the bare session id are not found. A host that set
  `langgraph_config.configurable.thread_id` itself sets `session_id` instead.
- OpenAI Agents no longer sends `conversation_id=session_id` (the Responses API rejects an id
  that is not a server conversation it created); pass a `conversation_id` mapping to the
  constructor to use server-managed conversations. `context`, `conversation_id`,
  `previous_response_id`, `session` and `run_config` in `openai_agents_options` are refused.
- `AutoGenAdapter` advertises `persistence=False` and `human_in_the_loop=False`, resets a shared
  instance before each run and runs one request at a time on it; pass `factory=` for concurrent
  runs.
- `CrewAIAdapter` advertises `streaming=False`, and `cancellation=False` unless the crew has
  native `akickoff`, which it now prefers (`kickoff_async` is `kickoff` in a worker thread).
- `GovernedToolBridge` refuses `write`, `external` and `destructive` tools by default (see
  below).

## Governing tool calls inside a framework

Hand Runtime tools to the framework through `GovernedToolBridge`, and each call to them goes
through the Runtime's tool executor: permissions, input validation, `before_tool` and `after_tool`
policy, guards, idempotency, the execution ledger and tool spans apply as they do in a Runtime run.

```python
from algen_agent_runtime.frameworks import GovernedToolBridge

bridge = GovernedToolBridge(
    container.runtime.tool_executor,
    container.tools,
    request,
    permissions={"crm.read"},
    policies=container.runtime.policies,  # optional: the deployment's plan_tool policies
)
agent = Agent(name="analyst", tools=bridge.openai_agents_tools("crm.find_account"))  # OpenAI Agents SDK
tools = bridge.langchain_tools("crm.find_account")  # LangGraph / LangChain
```

Before the executor runs a call, the bridge checks it as a Runtime run checks it at `plan_tool`
(since 0.2.0b4). A framework cannot pause for a Runtime approval, so a call that would need one
is refused with `PolicyDeniedError`:

- `approval_policy` (default `ApprovalPolicy()`) decides which calls need an approval. The
  default asks for one before every `write`, `external` and `destructive` tool, so the bridge
  refuses them. Pass `approval_policy=ApprovalPolicy(require_for_side_effects=False,
  require_for_destructive=False)` only when the framework gates those tools itself (for example
  with a LangGraph `interrupt()` before the call). A tool with `requires_approval=True` is always
  refused.
- `policies` (for example `container.runtime.policies`) is evaluated at `plan_tool`: `deny`,
  `require_approval`, `redact` and `require_clarification` refuse the call, and `transform`
  rewrites its arguments.
- Every call gets a random idempotency key (`<run_id>:framework:<uuid>:<tool>`), so two bridges
  for the same run never replay each other's results. Before 0.2.0b4 the key was a per-bridge
  counter.

What the Runtime enforces inside each framework:

| Call inside the framework | Governed by the Runtime | How |
| --- | --- | --- |
| A Runtime `read` (or `none`) tool handed over through the bridge | Yes: permissions, `plan_tool` and `before_tool` policy, validation, guards, ledger, spans | `bridge.openai_agents_tools(...)`, `bridge.langchain_tools(...)`; for AutoGen and CrewAI, wrap `await bridge.call(name, arguments)` in the framework's own tool type |
| A Runtime `write`, `external` or `destructive` tool | Refused by default (0.2.0b4; earlier versions ran it without an approval) | Allowed only with an `approval_policy` that does not require an approval for it |
| A Runtime tool that `requires_approval` | Refused | A foreign framework cannot pause a Runtime run; keep such tools in a Runtime agent or workflow |
| The framework's own tools | No: run and traced by the framework | Framework-native hooks |
| The framework's model calls | No: not routed, metered or guarded by the Runtime | Framework-native configuration; shared tracing (for example Traccia) |
| The outer call (the adapter) | Yes: tenancy, deadline, cancellation, identity propagation | `container.frameworks` |

`tests/unit/test_framework_governed_tools.py` pins this boundary with the real OpenAI Agents SDK
and `langchain_core` when they are installed, so an upgrade that bypasses it fails CI. The
`pattern_openai_agents_integration` example runs both kinds of tool side by side, offline.

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

- **LangGraph**: Uses `input_factory` (defaults to `{"messages": [{"role": "user", "content": request.input}]}`) and `output_selector`. `configurable.thread_id` is `"<tenant_id>:<session_id>"` (`LangGraphAdapter.thread_id(request)`; before 0.2.0b4 it was `session_id`), and metadata contains `run_id`, `tenant_id`, `user_id` and `session_id`.
- **OpenAI Agents SDK**: Accepts an application-owned SDK `Agent` plus optional `Runner` and `RunConfig`. Sets `run_id`, `tenant_id`, `user_id`, and `session_id` in the run context and trace metadata. Defaults to `RunConfig(tracing_disabled=True)` to ensure tracing does not leak or upload unexpectedly; pass your own `run_config` to the constructor to change that. Interruptions are surfaced as `FrameworkRunStatus.AWAITING_INPUT` with each tool call's id and arguments, and the SDK's `RunResult` is kept in `resume_state` (`capabilities.persistence=False`: it lives in the process only). A runnable offline example is provided in `algen_agent_runtime_examples/pattern_openai_agents_integration`.
- **AutoGen**: `AutoGenAdapter(factory=lambda request: build_team(request))` (preferred, since 0.2.0b4) builds an agent or team per request; `AutoGenAdapter(team)` shares one instance, which is `reset()` before each run and runs one request at a time. `input_factory` maps the request to the `task` (default `request.input`). Runs get a `CancellationToken` when `autogen_core` is installed and the agent's `run` accepts one.
- **CrewAI**: Accepts a `Crew`; default input mapping is `{"input": request.input}`. A `pydantic` or `json_dict` result is kept as JSON data in `metadata["value"]`.

For consuming external tools across standard language-agnostic boundaries (rather than embedding external agent graphs), see the [Model Context Protocol (MCP) Integration guide](mcp.md).

## Evidence & Maturity Matrix

| Framework | Status | Test Evidence | Real SDK Execution | CI Validation |
|---|:---:|---|:---:|---|
| **LangGraph** | Supported | Unit tests, contract cancellation/streaming, surface test | Yes (`test_real_langgraph_execution_and_streaming`) | Matrix test |
| **OpenAI Agents SDK** | Supported | Real SDK `ScriptedModel` suite (invoke, tool loop, stream, cancel, metadata), missing-dependency contract | Yes (credential-free `ScriptedModel` suite) | Optional integration (`frameworks`) |
| **AutoGen AgentChat** | Experimental | Unit tests with mock agents/teams, SDK surface test | No (network-dependent / no hermetic runner) | Optional integration (`frameworks`) |
| **CrewAI** | Experimental | Unit tests with mock crews, SDK surface test | No (network-dependent / no hermetic runner) | Optional integration (`frameworks`) |

## Hook Providers and Framework Adapters

When integrating external framework graphs within Algen workflows, register framework adapters during host initialization or inside a secure workflow hook provider. When loading hook providers dynamically from YAML manifests, use `WorkflowHookLoader` with explicit module allowlists to ensure foreign framework modules are loaded only from trusted packages.

