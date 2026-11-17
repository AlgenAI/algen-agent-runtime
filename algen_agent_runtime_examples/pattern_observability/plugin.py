from __future__ import annotations

from typing import Any

from algen_agent_runtime.plugins.sdk import PluginManifest, plugin
from algen_agent_runtime_examples.pattern_observability.tools import audit, level, reorder


@plugin(PluginManifest(name="example.stock", version="1.0.0", publisher="algen-examples"))
class StockPlugin:
    """Registers the stock tools; agent.yaml names this plugin in domain_plugins."""

    def register(self, context: Any) -> None:
        context.tools.register(level)
        context.tools.register(reorder)
        context.tools.register(audit)
