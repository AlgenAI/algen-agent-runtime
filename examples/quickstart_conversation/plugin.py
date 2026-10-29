from __future__ import annotations

from typing import Any

from algen_agent_runtime.plugins.sdk import PluginManifest, plugin
from examples.quickstart_conversation.tools import change_address, track


@plugin(PluginManifest(name="example.orders", version="1.0.0", publisher="algen-examples"))
class OrdersPlugin:
    """Registers the order tools; agent.yaml names this plugin in domain_plugins."""

    def register(self, context: Any) -> None:
        context.tools.register(track)
        context.tools.register(change_address)
