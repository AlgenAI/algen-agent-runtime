from __future__ import annotations

from collections.abc import Callable
from importlib import metadata
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.exceptions.errors import ConfigurationError


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    version: str
    core_api_version: str = "1"
    publisher: str
    signature: str | None = None
    capabilities: frozenset[str] = Field(default_factory=frozenset)


class PluginContext(Protocol):
    tools: object
    planners: object
    contexts: object
    verifiers: object
    semantics: object


class AlgenAgentRuntimePlugin(Protocol):
    manifest: PluginManifest

    def register(self, context: PluginContext) -> None: ...


class PluginLoader:
    def __init__(self, trusted_prefixes: tuple[str, ...]) -> None:
        self._trusted_prefixes = trusted_prefixes

    def load(self, names: tuple[str, ...], context: PluginContext) -> tuple[PluginManifest, ...]:
        if not self._trusted_prefixes and names:
            raise ConfigurationError("dynamic plugin loading requires trusted_plugin_prefixes")
        discovered = {
            entry.name: entry
            for entry in metadata.entry_points(group="algen_agent_runtime.plugins")
        }
        manifests = []
        for name in names:
            if not name.startswith(self._trusted_prefixes):
                raise ConfigurationError(f"plugin {name!r} is not from a trusted namespace")
            if name not in discovered:
                raise ConfigurationError(f"plugin entry point {name!r} was not found")
            plugin = discovered[name].load()()
            if plugin.manifest.core_api_version != "1":
                raise ConfigurationError(f"plugin {name!r} targets an incompatible core API")
            plugin.register(context)
            manifests.append(plugin.manifest)
        return tuple(manifests)


def plugin(manifest: PluginManifest) -> Callable[[type], type]:
    def decorate(cls: type) -> type:
        setattr(cls, "manifest", manifest)  # noqa: B010 - decorator attaches declared metadata
        return cls

    return decorate
