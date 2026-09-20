from __future__ import annotations

from algen_agent_runtime.exceptions.errors import ConflictError, NotFoundError
from algen_agent_runtime.tools.contracts import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = tool.definition.name
        if name in self._tools:
            raise ConflictError(f"tool {name!r} already registered")
        self._tools[name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise NotFoundError(f"tool {name!r} is not registered") from exc

    def list(self) -> tuple[Tool, ...]:
        return tuple(self._tools[name] for name in sorted(self._tools))
