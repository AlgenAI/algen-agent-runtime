from __future__ import annotations

from typing import Any

from algen_agent_runtime.workflows import WorkflowHookRegistry

INVOICE_SCHEMA = {
    "type": "object",
    "required": ["invoice_id", "po_id", "amount", "currency"],
    "properties": {
        "invoice_id": {"type": "string"},
        "po_id": {"type": "string"},
        "amount": {"type": "number"},
        "currency": {"type": "string"},
    },
}
ROUTE_SCHEMA = {
    "type": "object",
    "required": ["status", "question", "reason"],
    "properties": {
        "status": {"type": "string"},
        "question": {"type": ["string", "null"]},
        "reason": {"type": "string"},
    },
}


def create_hooks(**_: Any) -> WorkflowHookRegistry:
    hooks = WorkflowHookRegistry()
    hooks.register_schema("invoice.invoice_schema", INVOICE_SCHEMA)
    hooks.register_schema("invoice.route_schema", ROUTE_SCHEMA)
    hooks.register_handler(
        "invoice.screen",
        lambda context: {"safe": True, "findings": [], "document_id": "DOC-100"},
    )
    hooks.register_builder(
        "invoice.extract",
        lambda context: {
            "invoice_id": "INV-100",
            "po_id": "PO-100",
            "amount": 1250.0,
            "currency": "USD",
        },
    )
    hooks.register_validator(
        "invoice.extract_validator",
        lambda value, context: (
            None if float(value.get("amount", 0)) > 0 else "invoice amount must be positive"
        ),
    )
    hooks.register_handler(
        "invoice.po_match",
        lambda context: {
            "matched": True,
            "po_amount": 1200.0,
            "variance": 50.0,
            "evidence_id": "PO-100",
        },
    )
    hooks.register_handler(
        "invoice.duplicate",
        lambda context: {"duplicate": False, "evidence_id": "LEDGER-100"},
    )

    def route(context: Any) -> dict[str, Any]:
        answered = bool(context.state.values.get("clarifications"))
        return {
            "status": "approved_for_recommendation" if answered else "needs_review",
            "question": None
            if answered
            else "Approve the documented USD 50 purchase-order variance?",
            "reason": "amount variance requires separation-of-duties review",
        }

    hooks.register_builder("invoice.route", route)

    def recommend(context: Any) -> dict[str, Any]:
        answers = context.state.values.get("clarifications", [])
        approved = bool(answers) and str(answers[-1][1]).lower() in {"approve", "approved", "yes"}
        return {
            "invoice_id": context.state.values["invoice"]["invoice_id"],
            "recommendation": "release_for_payment_review" if approved else "hold",
            "payment_executed": False,
            "idempotency_key": f"invoice:{context.state.id}",
        }

    hooks.register_handler("invoice.recommend", recommend)
    return hooks
