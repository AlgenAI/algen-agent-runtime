from __future__ import annotations

from typing import Any

from algen_agent_runtime.plugins.sdk import PluginManifest, plugin

from .policies import AcademicIntegrityPolicy, EducationalEquityPolicy
from .tools import register_teaching_tools


@plugin(
    PluginManifest(name="example.teaching-assistant", version="1.0.0", publisher="algen-examples")
)
class TeachingPlugin:
    """Registers the teaching.* tools and the education guardrail policies that
    config/agent.yaml enables; the config names this plugin in domain_plugins."""

    def register(self, context: Any) -> None:
        register_teaching_tools(context.tools)
        context.policies.register(AcademicIntegrityPolicy())
        context.policies.register(EducationalEquityPolicy())
