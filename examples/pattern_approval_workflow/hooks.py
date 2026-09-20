from __future__ import annotations

from typing import Any

from algen_agent_runtime.workflows import WorkflowHookRegistry

SCHEMA = {
    "type": "object",
    "required": ["status", "question", "requested_change"],
    "properties": {
        "status": {"type": "string"},
        "question": {"type": ["string", "null"]},
        "requested_change": {"type": "string"},
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
        answered = bool(context.state.values.get("clarifications"))
        return {
            "status": "ready" if answered else "needs_clarification",
            "question": None if answered else "Approve enabling the synthetic account credit?",
            "requested_change": str(context.state.values["question"]),
        }

    hooks.register_builder("crm.propose", proposal)

    def update(context: Any) -> dict[str, Any]:
        answers = context.state.values.get("clarifications", [])
        decision = str(answers[-1][1]).strip().lower() if answers else "reject"
        approved = decision in {"approve", "approved", "yes"}
        return {
            "customer_id": context.state.values["customer"]["customer_id"],
            "status": "updated" if approved else "rejected",
            "idempotency_key": f"crm:{context.state.id}",
        }

    hooks.register_handler("crm.update", update)
    return hooks
