from __future__ import annotations

from typing import Any

from algen_agent_runtime.plugins.sdk import PluginManifest, plugin
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition


@plugin(PluginManifest(name="example.utilities", version="1.0.0", publisher="example"))
class UtilitiesPlugin:
    def register(self, context: Any) -> None:
        async def word_count(
            arguments: dict[str, Any], tool_context: ToolContext
        ) -> dict[str, int]:
            return {"count": len(arguments["text"].split())}

        context.tools.register(
            Tool(
                ToolDefinition(
                    name="text.word_count",
                    version="1.0.0",
                    description="Count whitespace-separated words.",
                    input_schema={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                    output_schema={
                        "type": "object",
                        "properties": {"count": {"type": "integer"}},
                        "required": ["count"],
                    },
                    side_effect=SideEffect.NONE,
                ),
                word_count,
            )
        )
