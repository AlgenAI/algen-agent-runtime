"""The reader tool the agent hands an attached CSV to; agent.yaml names this plugin."""

from __future__ import annotations

from typing import Any

from algen_agent_runtime.plugins.sdk import PluginManifest, plugin
from algen_agent_runtime.tools.authoring import read, tool
from algen_agent_runtime.tools.contracts import ToolContext


@tool(read(), name="files.read_text")
async def read_text(artifact_id: str, context: ToolContext) -> dict[str, Any]:
    """Read an attached text file (a CSV, say) by the id in its [attachment: ...] placeholder."""
    attached = await context.read_attachment(artifact_id)
    text = attached.data.decode("utf-8")
    return {"name": attached.name, "rows": len(text.splitlines()) - 1, "text": text}


@plugin(PluginManifest(name="example.files", version="1.0.0", publisher="algen-examples"))
class FilesPlugin:
    """Registers files.read_text."""

    def register(self, context: Any) -> None:
        context.tools.register(read_text)
