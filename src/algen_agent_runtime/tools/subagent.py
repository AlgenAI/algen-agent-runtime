from __future__ import annotations

from typing import Any

from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.types.contracts import RunRequest
from algen_agent_runtime.types.interfaces import Runtime


def subagent_tool(runtime: Runtime, max_depth: int = 3) -> Tool:
    async def execute(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        depth = int(arguments.get("depth", 0))
        if depth >= max_depth:
            raise ValueError(f"sub-agent nesting limit {max_depth} reached")
        result = await runtime.run(
            RunRequest(
                agent=arguments["agent"],
                agent_version=arguments.get("agent_version"),
                input=arguments["input"],
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                metadata={"parent_run_id": context.run_id, "depth": str(depth + 1)},
            )
        )
        return result.model_dump(mode="json")

    return Tool(
        ToolDefinition(
            name="core.subagent",
            version="1.0.0",
            description="Delegate a bounded task to another registered agent in the same tenant.",
            input_schema={
                "type": "object",
                "properties": {
                    "agent": {"type": "string"},
                    "agent_version": {"type": "string"},
                    "input": {"type": "string", "minLength": 1},
                    "depth": {"type": "integer", "minimum": 0},
                },
                "required": ["agent", "input"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "required": ["run_id", "status", "session_id"]},
            required_permissions=frozenset({"agents.delegate"}),
            side_effect=SideEffect.NONE,
        ),
        execute,
    )
