from __future__ import annotations

from typing import Any

from algen_agent_runtime.workflows import WorkflowHookRegistry

ANSWER_SCHEMA = {
    "type": "object",
    "required": ["summary", "citations"],
    "properties": {
        "summary": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "string"}},
    },
}


def create_hooks(**_: Any) -> WorkflowHookRegistry:
    hooks = WorkflowHookRegistry()
    hooks.register_schema("research.answer_schema", ANSWER_SCHEMA)
    hooks.register_handler(
        "research.collect",
        lambda context: [
            {
                "id": "S1",
                "title": "Runtime workflow contract",
                "text": "Runtime owns DAG scheduling, bounded repair, fan-out, and lifecycle events.",
            },
            {
                "id": "S2",
                "title": "Application boundary",
                "text": "Applications own domain prompts, topology, validators, and service adapters.",
            },
        ],
    )
    hooks.register_builder(
        "research.synthesize",
        lambda context: {
            "summary": (
                f"For '{context.state.values['question']}', Runtime owns orchestration while "
                "applications own domain behavior [S1] [S2]."
            ),
            "citations": ["S1", "S2"],
        },
    )

    def verify(context: Any) -> dict[str, Any]:
        answer = context.state.values["answer"]
        allowed = {item["id"] for item in context.state.values["evidence"]}
        supplied = set(answer["citations"])
        return {**answer, "verified": bool(supplied) and supplied <= allowed}

    hooks.register_handler("research.verify", verify)
    return hooks
