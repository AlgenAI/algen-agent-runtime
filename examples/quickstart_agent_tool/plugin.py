from __future__ import annotations

from typing import Any

from algen_agent_runtime.plugins.sdk import PluginManifest, plugin
from examples.quickstart_agent_tool.tools import crm


@plugin(PluginManifest(name="example.crm", version="1.0.0", publisher="algen-examples"))
class CrmPlugin:
    """Registers the crm toolkit; agent.yaml names this plugin in domain_plugins."""

    def register(self, context: Any) -> None:
        crm.register(context)
