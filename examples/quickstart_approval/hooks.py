from __future__ import annotations

from typing import Any

from algen_agent_runtime.workflows import WorkflowHookRegistry

PROPOSAL_SCHEMA = {
    "type": "object",
    "required": ["status", "change", "question"],
    "properties": {
        "status": {"type": "string"},
        "change": {"type": "string"},
        "question": {"type": ["string", "null"]},
    },
}


def create_hooks(**_: Any) -> WorkflowHookRegistry:
    hooks = WorkflowHookRegistry()
    hooks.register_schema("approval.proposal_schema", PROPOSAL_SCHEMA)

    def propose(context: Any) -> dict[str, Any]:
        clarifications = context.state.values.get("clarifications", [])
        return {
            "status": "ready" if clarifications else "needs_approval",
            "change": str(context.state.values["question"]),
            "question": None
            if clarifications
            else "Approve this synthetic change? Reply approve or reject.",
        }

    def apply(context: Any) -> dict[str, Any]:
        answers = context.state.values.get("clarifications", [])
        answer = str(answers[-1][1]).strip().lower() if answers else "reject"
        approved = answer in {"approve", "approved", "yes"}
        return {
            "status": "applied" if approved else "rejected",
            "idempotency_key": f"approval:{context.state.id}",
            "side_effect": "synthetic-only" if approved else "none",
        }

    hooks.register_builder("approval.propose", propose)
    hooks.register_handler("approval.apply", apply)
    return hooks
