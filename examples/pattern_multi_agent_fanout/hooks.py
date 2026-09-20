from __future__ import annotations

from typing import Any

from algen_agent_runtime.workflows import WorkflowHookRegistry

PLAN_SCHEMA = {
    "type": "object",
    "required": ["tasks"],
    "properties": {
        "tasks": {"type": "array", "maxItems": 3, "items": {"type": "object"}},
    },
}
FINDING_SCHEMA = {
    "type": "object",
    "required": ["topic", "finding", "source"],
    "properties": {
        "topic": {"type": "string"},
        "finding": {"type": "string"},
        "source": {"type": "string"},
    },
}
ANSWER_SCHEMA = {
    "type": "object",
    "required": ["summary", "findings"],
    "properties": {"summary": {"type": "string"}, "findings": {"type": "array"}},
}


def create_hooks(**_: Any) -> WorkflowHookRegistry:
    hooks = WorkflowHookRegistry()
    hooks.register_schema("fanout.plan_schema", PLAN_SCHEMA)
    hooks.register_schema("fanout.finding_schema", FINDING_SCHEMA)
    hooks.register_schema("fanout.answer_schema", ANSWER_SCHEMA)

    def plan(context: Any) -> dict[str, Any]:
        question = str(context.state.values["question"])
        tasks = [
            {"topic": "facts", "question": question},
            {"topic": "risks", "question": question},
            {"topic": "recommendations", "question": question},
        ]
        if context.validation_error:
            tasks = tasks[:3]
        return {"tasks": tasks}

    hooks.register_builder("fanout.plan", plan)
    hooks.register_validator(
        "fanout.plan_validator",
        lambda value, context: (
            None if 0 < len(value.get("tasks", [])) <= 3 else "provide between one and three tasks"
        ),
    )
    hooks.register_builder(
        "fanout.research",
        lambda context: {
            "topic": context.item["topic"],
            "finding": f"Synthetic bounded finding for {context.item['topic']}",
            "source": f"synthetic://{context.item_index}",
        },
    )
    hooks.register_builder(
        "fanout.synthesize",
        lambda context: {
            "summary": f"Completed bounded parallel research for: {context.state.values['question']}",
            "findings": context.state.values["findings"],
        },
    )
    return hooks
