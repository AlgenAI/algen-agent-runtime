from __future__ import annotations

from typing import Any

from algen_agent_runtime.plugins.sdk import PluginManifest, plugin
from examples.pattern_text_to_sql.app import ReadOnlyAnalyticsDatabase, create_query_tool


@plugin(PluginManifest(name="example.text-to-sql", version="1.0.0", publisher="algen-examples"))
class AnalyticsPlugin:
    """Registers analytics.query; agent.yaml names this plugin in domain_plugins."""

    def register(self, context: Any) -> None:
        database = ReadOnlyAnalyticsDatabase()
        context.add_resource(database)
        context.tools.register(create_query_tool(database))
