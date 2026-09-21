from __future__ import annotations

from typing import Any

from algen_agent_runtime.workflows import WorkflowHookRegistry


def create_hooks(**_: Any) -> WorkflowHookRegistry:
    hooks = WorkflowHookRegistry()

    def child_input(context: Any) -> dict[str, str]:
        return {"request": str(context.state.values["question"])}

    def perform(context: Any) -> dict[str, str]:
        request = str(context.state.values["inputs"]["request"])
        return {"status": "completed", "summary": f"Child handled: {request}"}

    hooks.register_builder("composition.child_input", child_input)
    hooks.register_handler("composition.perform", perform)
    return hooks
