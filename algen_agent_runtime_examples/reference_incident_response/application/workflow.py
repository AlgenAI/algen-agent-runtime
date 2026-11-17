from __future__ import annotations

from typing import Any

from algen_agent_runtime.workflows import WorkflowHookRegistry


def _schema(required: list[str]) -> dict[str, Any]:
    return {"type": "object", "required": required, "additionalProperties": True}


def create_hooks(**_: Any) -> WorkflowHookRegistry:
    hooks = WorkflowHookRegistry()
    hooks.register_schema("incident.classification_schema", _schema(["severity", "service"]))
    hooks.register_schema("incident.analysis_schema", _schema(["cause", "confidence"]))
    hooks.register_schema("incident.plan_schema", _schema(["status", "question", "action"]))
    hooks.register_schema("incident.report_schema", _schema(["summary", "evidence_ids"]))
    hooks.register_builder(
        "incident.classify",
        lambda context: {
            "severity": "high",
            "service": "checkout",
            "alert": context.state.values["question"],
        },
    )
    hooks.register_handler(
        "incident.logs",
        lambda context: {"source": "LOG-1", "errors": 37, "message": "database timeout"},
    )
    hooks.register_handler(
        "incident.metrics",
        lambda context: {"source": "METRIC-1", "error_rate": 0.18, "latency_ms": 2400},
    )
    hooks.register_builder(
        "incident.analyze",
        lambda context: {
            "cause": "database connection saturation",
            "confidence": 0.86,
            "evidence": ["LOG-1", "METRIC-1"],
        },
    )

    def plan(context: Any) -> dict[str, Any]:
        # The approve-remediation node asks the reviewer; the plan only proposes.
        return {
            "status": "proposed",
            "question": "Approve the synthetic connection-pool rollback?",
            "action": "rollback_connection_pool_config",
        }

    hooks.register_builder("incident.plan", plan)

    def execute(context: Any) -> dict[str, Any]:
        # The decision the approve-remediation node recorded (its output_key). Only a decision
        # that approves runs the write; a rejection skips this node (skip_dependents).
        decision = (context.state.values.get("remediation_approval") or {}).get("decision")
        approved = decision in {"approved", "modified"}
        return {
            "status": "simulated" if approved else "rejected",
            "synthetic": True,
            "idempotency_key": f"incident:{context.state.id}",
        }

    hooks.register_handler("incident.execute", execute)
    hooks.register_builder(
        "incident.report",
        lambda context: {
            "summary": f"Checkout incident remediation was {context.state.values['execution']['status']}.",
            "evidence_ids": ["LOG-1", "METRIC-1"],
        },
    )
    return hooks
