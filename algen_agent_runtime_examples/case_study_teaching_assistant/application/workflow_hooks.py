from __future__ import annotations

from typing import Any

from algen_agent_runtime.workflows import WorkflowHookRegistry


def create_hooks(**_: Any) -> WorkflowHookRegistry:
    hooks = WorkflowHookRegistry()
    hooks.register_builder("teaching.question", lambda context: context.state.values["question"])
    return hooks
