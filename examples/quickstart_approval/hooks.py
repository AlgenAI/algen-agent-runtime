from __future__ import annotations

from typing import Any

from algen_agent_runtime.workflows import WorkflowHookRegistry

PROPOSAL_SCHEMA = {
    "type": "object",
    "required": ["change", "parameters"],
    "properties": {
        "change": {"type": "string"},
        "parameters": {
            "type": "object",
            "required": ["change"],
            "properties": {"change": {"type": "string"}},
        },
    },
}


def create_hooks(**_: Any) -> WorkflowHookRegistry:
    hooks = WorkflowHookRegistry()
    hooks.register_schema("approval.proposal_schema", PROPOSAL_SCHEMA)

    def propose(context: Any) -> dict[str, Any]:
        change = str(context.state.values["question"])
        return {
            "change": change,
            "parameters": {"change": change},
        }

    def apply(context: Any) -> dict[str, Any]:
        approval = context.state.values["approval"]
        return {
            "status": "applied",
            "parameters": approval["parameters"],
            "idempotency_key": f"approval:{context.state.id}",
            "side_effect": "synthetic-only",
        }

    hooks.register_builder("approval.propose", propose)
    hooks.register_handler("approval.apply", apply)
    return hooks
