from __future__ import annotations

from typing import Any

from algen_agent_runtime.workflows import WorkflowHookRegistry

RESULT_SCHEMA = {
    "type": "object",
    "required": ["sku", "available", "warehouse"],
    "properties": {
        "sku": {"type": "string"},
        "available": {"type": "integer"},
        "warehouse": {"type": "string"},
    },
}


def create_hooks(**_: Any) -> WorkflowHookRegistry:
    hooks = WorkflowHookRegistry()
    hooks.register_schema("inventory.result_schema", RESULT_SCHEMA)

    def lookup(context: Any) -> dict[str, Any]:
        requested = str(context.state.values.get("question", "SKU-100")).upper()
        sku = "SKU-200" if "200" in requested else "SKU-100"
        return {"sku": sku, "available": 7 if sku == "SKU-200" else 42, "warehouse": "demo-east"}

    hooks.register_handler("inventory.lookup", lookup)
    hooks.register_builder("inventory.present", lambda context: context.state.values["inventory"])
    return hooks
