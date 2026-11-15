from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from algen_agent_runtime.exceptions.errors import ConflictError, NotFoundError
from algen_agent_runtime.types.contracts import AgentDefinition, RunState


def _version_key(version: str) -> tuple[int, int, int, str]:
    core, _, suffix = version.partition("-")
    parts = core.partition("+")[0].split(".")
    return int(parts[0]), int(parts[1]), int(parts[2]), suffix


class InMemoryAgentRegistry:
    """The agents a Runtime runs, by name and version.

    Every version of a name has the owner of the name's first registration: ``None`` for
    agents the host registers (configuration, ``build_container``, code), or the owner a caller
    names, such as ``http:<tenant>`` for ``POST /v1/agents``. A version of a name with another
    owner is refused (0.2.0b4), so a registration can never add a higher version of an agent
    someone else defined and take over its runs, which resolve the highest version when the
    request names none.
    """

    def __init__(self) -> None:
        self._definitions: dict[str, AgentDefinition] = {}
        self._owners: dict[str, str | None] = {}

    def register(self, definition: AgentDefinition, *, owner: str | None = None) -> None:
        if definition.key in self._definitions:
            raise ConflictError(f"agent {definition.key!r} already registered")
        if definition.name in self._owners and self._owners[definition.name] != owner:
            raise ConflictError(
                f"agent {definition.name!r} is defined by another owner; "
                "register a version under a new name"
            )
        self._definitions[definition.key] = definition
        self._owners.setdefault(definition.name, owner)

    def owner(self, name: str) -> str | None:
        """Who registered ``name`` first: None for the host, else the owner it was given."""
        if name not in self._owners:
            raise NotFoundError(f"agent {name!r} not found")
        return self._owners[name]

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


class _AgentLookup(Protocol):
    def get(self, name: str, version: str | None = None) -> AgentDefinition: ...


def pinned_agent(agents: _AgentLookup, state: RunState) -> AgentDefinition | None:
    """The exact definition ``state`` started with (``RunState.agent_key``, set by
    ``AgentRuntime.start``), or None when the run records none (a run stored by a release that
    did not) or that version is no longer registered. Resolving a paused or recovered run
    through it keeps a version registered later from taking the run over (0.2.0b4)."""
    key = state.agent_key
    prefix = f"{state.request.agent}@"
    if not key or not key.startswith(prefix):
        return None
    try:
        return agents.get(state.request.agent, key.removeprefix(prefix))
    except NotFoundError:
        return None
