"""Runtime workflow hooks for Studio and other generic workflow hosts.

The existing dashboard remains an application-specific host, while this provider exposes the same
specialist topology through Runtime's portable DAG contract.
"""

from __future__ import annotations

from typing import Any

from algen_agent_runtime.workflows import WorkflowHookRegistry
from examples.case_study_responsible_hiring.application.contracts import (
    BiasReport,
    CommunicationDraft,
    FraudReport,
    GatekeeperReport,
    ScreeningReport,
)


def create_hooks(**_: Any) -> WorkflowHookRegistry:
    hooks = WorkflowHookRegistry()
    hooks.register_schema("hiring.gate_schema", GatekeeperReport.model_json_schema())
    hooks.register_schema("hiring.fraud_schema", FraudReport.model_json_schema())
    hooks.register_schema("hiring.screen_schema", ScreeningReport.model_json_schema())
    hooks.register_schema("hiring.bias_schema", BiasReport.model_json_schema())
    hooks.register_schema("hiring.communication_schema", CommunicationDraft.model_json_schema())
    hooks.register_builder(
        "hiring.gate_input",
        lambda context: {
            "document_disposition": "review",
            "deterministic_findings": [],
            "sanitized_identity_free_resume": context.state.values["question"],
        },
    )
    hooks.register_builder(
        "hiring.fraud_input",
        lambda context: {
            "resume": context.state.values["question"],
            "instruction": "Report signals, not accusations.",
        },
    )
    hooks.register_builder(
        "hiring.screen_input",
        lambda context: {
            "job_description": "Evaluate only evidence supplied in this Studio run.",
            "resume": context.state.values["question"],
        },
    )
    hooks.register_builder(
        "hiring.bias_input",
        lambda context: {
            "screening_report": context.state.values["screening"],
            "fraud_report": context.state.values["fraud"],
            "identity_data_available": False,
        },
    )
    hooks.register_builder(
        "hiring.communication_input",
        lambda context: {
            "screening_report": context.state.values["screening"],
            "bias_report": context.state.values["bias"],
            "instruction": "Draft for human review; do not send.",
        },
    )
    return hooks
