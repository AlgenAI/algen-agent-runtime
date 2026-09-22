from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from algen_agent_runtime.tools.contracts import Tool
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.tools.starter.calculator import calculator_tool
from algen_agent_runtime.tools.starter.clock import clock_tool
from algen_agent_runtime.tools.starter.fs import directory_list_tool, file_read_tool
from algen_agent_runtime.tools.starter.json_query import json_query_tool

__all__ = [
    "calculator_tool",
    "clock_tool",
    "directory_list_tool",
    "file_read_tool",
    "get_starter_tools",
    "json_query_tool",
    "register_starter_tools",
]


def get_starter_tools(
    workspace_root: Path | str | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> tuple[Tool, ...]:
    """Return an immutable tuple of standard safe starter tools."""
    return (
        calculator_tool(),
        json_query_tool(),
        file_read_tool(workspace_root=workspace_root),
        directory_list_tool(workspace_root=workspace_root),
        clock_tool(now_fn=now_fn),
    )


def register_starter_tools(
    registry: ToolRegistry,
    workspace_root: Path | str | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> None:
    """Register the starter tools into a ToolRegistry."""
    for tool in get_starter_tools(workspace_root=workspace_root, now_fn=now_fn):
        registry.register(tool)
