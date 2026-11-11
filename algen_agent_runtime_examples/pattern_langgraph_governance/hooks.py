from __future__ import annotations

from typing import Any

from algen_agent_runtime.frameworks import FrameworkRunRequest, LangGraphAdapter
from algen_agent_runtime.workflows import WorkflowHookRegistry


class ExampleGraph:
    """Small protocol-compatible graph; replace it with a compiled LangGraph graph unchanged."""

    async def ainvoke(self, value: dict[str, Any], *, config: dict[str, Any]) -> dict[str, Any]:
        question = value["messages"][-1]["content"]
        identity = config["metadata"]
        return {
            "answer": f"Graph processed: {question}",
            "run_id": identity["run_id"],
            "tenant_id": identity["tenant_id"],
        }


def create_hooks(**_: Any) -> WorkflowHookRegistry:
    hooks = WorkflowHookRegistry()
    adapter = LangGraphAdapter(ExampleGraph(), output_selector=lambda value: value["answer"])

    async def invoke(context: Any) -> dict[str, Any]:
        state = context.state
        result = await adapter.invoke(
            FrameworkRunRequest(
                input=str(state.values["question"]),
                run_id=state.id,
                tenant_id=state.tenant_id,
                user_id=state.user_id,
                session_id=state.conversation_id or state.id,
            )
        )
        return {
            "output": result.output,
            "status": result.status.value,
            "framework": adapter.framework_id,
        }

    hooks.register_handler("framework.langgraph", invoke)
    return hooks
