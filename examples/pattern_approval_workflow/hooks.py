from __future__ import annotations

from typing import Any

from algen_agent_runtime.workflows import WorkflowHookRegistry

SCHEMA = {
    "type": "object",
    "required": ["requested_change", "parameters"],
    "properties": {
        "requested_change": {"type": "string"},
        "parameters": {
            "type": "object",
            "required": ["customer_id", "credit_enabled"],
            "properties": {
                "customer_id": {"type": "string"},
                "credit_enabled": {"type": "boolean"},
            },
        },
    },
}


def create_hooks(**_: Any) -> WorkflowHookRegistry:
    hooks = WorkflowHookRegistry()
    hooks.register_schema("crm.proposal_schema", SCHEMA)
    hooks.register_handler(
        "crm.lookup",
        lambda context: {"customer_id": "C-100", "tier": "standard", "credit_enabled": False},
    )

    def proposal(context: Any) -> dict[str, Any]:
        return {
            "requested_change": str(context.state.values["question"]),
            "parameters": {
                "customer_id": context.state.values["customer"]["customer_id"],
                "credit_enabled": True,
            },
        }

    hooks.register_builder("crm.propose", proposal)

    def update(context: Any) -> dict[str, Any]:
        parameters = context.state.values["approval"]["parameters"]
        return {
            "customer_id": parameters["customer_id"],
            "credit_enabled": parameters["credit_enabled"],
            "status": "updated",
            "idempotency_key": f"crm:{context.state.id}",
        }

    hooks.register_handler("crm.update", update)
    return hooks
