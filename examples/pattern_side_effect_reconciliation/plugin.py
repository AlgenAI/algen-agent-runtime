from __future__ import annotations

from typing import Any

from algen_agent_runtime.plugins.sdk import PluginManifest, plugin
from examples.pattern_side_effect_reconciliation.tools import billing


@plugin(PluginManifest(name="example.billing", version="1.0.0", publisher="algen-examples"))
class BillingPlugin:
    """Registers the billing toolkit; agent.yaml names this plugin in domain_plugins."""

    def register(self, context: Any) -> None:
        billing.register(context)
