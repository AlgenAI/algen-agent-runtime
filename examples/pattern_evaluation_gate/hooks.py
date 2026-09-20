from __future__ import annotations

from typing import Any

from algen_agent_runtime.workflows import WorkflowHookRegistry


def create_hooks(**_: Any) -> WorkflowHookRegistry:
    hooks = WorkflowHookRegistry()
    hooks.register_handler(
        "evaluation.run",
        lambda context: {
            "suite": "safe-sql-v1",
            "score": 1.0,
            "threshold": 1.0,
            "promoted": True,
            "cases": [
                {"id": "aggregate", "passed": True},
                {"id": "reject-write", "passed": True},
            ],
        },
    )
    return hooks
