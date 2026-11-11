from __future__ import annotations

from typing import Any

from algen_agent_runtime.workflows import WorkflowHookRegistry

TRIAGE_SCHEMA = {
    "type": "object",
    "required": ["category", "status", "question", "requested_action"],
    "properties": {
        "category": {"type": "string"},
        "status": {"type": "string"},
        "question": {"type": ["string", "null"]},
        "requested_action": {"type": "string"},
    },
}
RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["message", "citations"],
    "properties": {"message": {"type": "string"}, "citations": {"type": "array"}},
}


def create_hooks(**_: Any) -> WorkflowHookRegistry:
    hooks = WorkflowHookRegistry()
    hooks.register_schema("support.triage_schema", TRIAGE_SCHEMA)
    hooks.register_schema("support.response_schema", RESPONSE_SCHEMA)

    def triage(context: Any) -> dict[str, Any]:
        question = str(context.state.values["question"])
        clarifications = context.state.values.get("clarifications", [])
        refund = "refund" in question.lower()
        return {
            "category": "billing" if refund else "general",
            "status": "ready" if clarifications or not refund else "needs_clarification",
            "question": None
            if clarifications or not refund
            else "What order ID should be reviewed?",
            "requested_action": "refund_review" if refund else "answer",
        }

    hooks.register_builder("support.triage", triage)
    hooks.register_handler(
        "support.knowledge",
        lambda context: {
            "source": "KB-REFUND-1",
            "policy": "Eligible refunds require an order ID and human review.",
        },
    )
    hooks.register_handler(
        "support.entitlement",
        lambda context: {"customer_id": "CUS-100", "plan": "standard", "refund_limit": 100},
    )
    hooks.register_builder(
        "support.draft",
        lambda context: {
            "message": "The request was checked against the refund policy and account entitlement [KB-REFUND-1].",
            "citations": ["KB-REFUND-1"],
        },
    )

    def action(context: Any) -> dict[str, Any]:
        requested = context.state.values["triage"]["requested_action"]
        return {
            "status": "queued_for_human_review"
            if requested == "refund_review"
            else "no_write_required",
            "idempotency_key": f"support:{context.state.id}",
            "external_write": False,
        }

    hooks.register_handler("support.action", action)
    return hooks
