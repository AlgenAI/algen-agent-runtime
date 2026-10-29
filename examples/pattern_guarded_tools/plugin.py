from __future__ import annotations

from typing import Any

from algen_agent_runtime.plugins.sdk import PluginManifest, plugin
from examples.pattern_guarded_tools.tools import shop_tools


@plugin(PluginManifest(name="example.shop", version="1.0.0", publisher="algen-examples"))
class ShopPlugin:
    """Registers the shop tools; agent.yaml names this plugin in domain_plugins."""

    def register(self, context: Any) -> None:
        for shop_tool in shop_tools():
            context.tools.register(shop_tool)
