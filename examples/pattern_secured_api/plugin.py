from __future__ import annotations

from typing import Any

from algen_agent_runtime.plugins.sdk import PluginManifest, plugin
from examples.pattern_secured_api.tools import webhooks


@plugin(PluginManifest(name="example.webhooks", version="1.0.0", publisher="algen-examples"))
class WebhookPlugin:
    """Registers the webhooks toolkit; agent.yaml names this plugin in domain_plugins."""

    def register(self, context: Any) -> None:
        webhooks.register(context)
