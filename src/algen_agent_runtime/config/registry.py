from __future__ import annotations

from collections.abc import Sequence

from algen_agent_runtime.exceptions.errors import ConflictError, NotFoundError
from algen_agent_runtime.types.contracts import AgentDefinition


def _version_key(version: str) -> tuple[int, int, int, str]:
    core, _, suffix = version.partition("-")
    parts = core.partition("+")[0].split(".")
    return int(parts[0]), int(parts[1]), int(parts[2]), suffix


class InMemoryAgentRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, AgentDefinition] = {}

    def register(self, definition: AgentDefinition) -> None:
        if definition.key in self._definitions:
            raise ConflictError(f"agent {definition.key!r} already registered")
        self._definitions[definition.key] = definition

    def get(self, name: str, version: str | None = None) -> AgentDefinition:
        if version:
            result = self._definitions.get(f"{name}@{version}")
        else:
            versions = [d for d in self._definitions.values() if d.name == name]
            result = max(versions, key=lambda d: _version_key(d.version), default=None)
        if result is None:
            raise NotFoundError(f"agent {name!r} version {version or 'latest'!r} not found")
        return result

    def list(self) -> Sequence[AgentDefinition]:
        return tuple(sorted(self._definitions.values(), key=lambda item: item.key))
