from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from algen_agent_runtime.exceptions.errors import NotFoundError, PolicyDeniedError
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)

MAX_READ_BYTES = 1_048_576  # 1 MB
DEFAULT_READ_BYTES = 262_144  # 256 KB
MAX_DIR_ENTRIES = 1_000
DEFAULT_DIR_ENTRIES = 200
MAX_DIR_DEPTH = 5


def _resolve_and_validate_path(workspace_root: Path, relative_path: str) -> Path:
    """Resolve a relative path against workspace root and verify strict containment."""
    resolved_root = workspace_root.resolve()

    # Reject explicit absolute paths or paths trying to escape root
    cleaned = relative_path.strip().lstrip("/\\")
    candidate = (resolved_root / cleaned).resolve()

    if not candidate.is_relative_to(resolved_root):
        raise PolicyDeniedError(
            f"access denied: path {relative_path!r} resolves outside workspace root"
        )

    # Check symlinks for path escape
    try:
        real_path = Path(os.path.realpath(candidate))
    except (OSError, ValueError) as exc:
        raise PolicyDeniedError(f"unable to verify filesystem path safety: {exc}") from exc

    if not real_path.is_relative_to(resolved_root):
        raise PolicyDeniedError(
            f"access denied: path {relative_path!r} is a symlink escaping workspace root"
        )

    return candidate


def _read_file_sync(target: Path, offset: int, max_bytes: int) -> bytes:
    with open(target, "rb") as f:
        if offset > 0:
            f.seek(offset)
        return f.read(max_bytes)


def _scan_dir_sync(
    target: Path,
    resolved_root: Path,
    max_entries: int,
    recursive: bool,
    max_depth: int,
) -> tuple[list[dict[str, Any]], bool]:
    entries: list[dict[str, Any]] = []
    truncated = False

    def scan_dir(dir_path: Path, current_depth: int) -> None:
        nonlocal truncated
        if current_depth > max_depth or truncated:
            return

        try:
            dir_iter = os.scandir(dir_path)
        except OSError as exc:
            raise PolicyDeniedError(f"cannot read directory {dir_path}: {exc}") from exc

        with dir_iter as it:
            for entry in sorted(it, key=lambda e: e.name):
                if len(entries) >= max_entries:
                    truncated = True
                    break

                entry_path = Path(entry.path)
                try:
                    real = entry_path.resolve()
                    if not real.is_relative_to(resolved_root):
                        continue
                except OSError:
                    continue

                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                    stat = entry.stat(follow_symlinks=False)
                    size_bytes = stat.st_size if is_file else 0
                except OSError:
                    continue

                rel_path = str(entry_path.resolve().relative_to(resolved_root))
                entry_type = "directory" if is_dir else ("file" if is_file else "other")
                entries.append(
                    {
                        "name": entry.name,
                        "path": rel_path,
                        "type": entry_type,
                        "size_bytes": size_bytes,
                    }
                )

                if recursive and is_dir and not entry.is_symlink():
                    scan_dir(entry_path, current_depth + 1)

    scan_dir(target, 1)
    return entries, truncated


def file_read_tool(workspace_root: Path | str | None = None) -> Tool:
    """Create a workspace-rooted safe text file reading tool."""
    root = Path(workspace_root) if workspace_root else Path.cwd()

    async def execute(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        req_path = arguments["path"]
        max_bytes = min(int(arguments.get("max_bytes", DEFAULT_READ_BYTES)), MAX_READ_BYTES)
        offset = max(0, int(arguments.get("offset", 0)))

        target = _resolve_and_validate_path(root, req_path)

        if not target.exists():
            raise NotFoundError(f"file not found: {req_path!r}")
        if not target.is_file():
            raise ValueError(f"path {req_path!r} is not a regular file")

        file_size = target.stat().st_size
        raw_data = await asyncio.to_thread(_read_file_sync, target, offset, max_bytes)

        # Check for binary content
        if b"\x00" in raw_data[:4096]:
            raise ValueError(f"file {req_path!r} contains binary data and cannot be read as text")

        try:
            content = raw_data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"file {req_path!r} is not valid UTF-8 encoded text: {exc.reason}"
            ) from exc

        return {
            "path": req_path,
            "content": content,
            "bytes_read": len(raw_data),
            "total_bytes": file_size,
            "truncated": file_size > (offset + len(raw_data)),
        }

    return Tool(
        ToolDefinition(
            name="starter.file_read",
            version="1.0.0",
            description="Read content from a text file strictly contained within the workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to the file within the workspace.",
                    },
                    "offset": {
                        "type": "integer",
                        "default": 0,
                        "description": "Byte offset from which to start reading.",
                    },
                    "max_bytes": {
                        "type": "integer",
                        "default": DEFAULT_READ_BYTES,
                        "description": "Maximum bytes to read (up to 1MB).",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "bytes_read": {"type": "integer"},
                    "total_bytes": {"type": "integer"},
                    "truncated": {"type": "boolean"},
                },
                "required": ["path", "content", "bytes_read", "total_bytes", "truncated"],
            },
            required_permissions=frozenset({"fs.read"}),
            side_effect=SideEffect.READ,
            idempotency=Idempotency.IDEMPOTENT,
        ),
        execute,
    )


def directory_list_tool(workspace_root: Path | str | None = None) -> Tool:
    """Create a workspace-rooted directory listing tool."""
    root = Path(workspace_root) if workspace_root else Path.cwd()

    async def execute(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        req_path = arguments.get("path", "")
        max_entries = min(int(arguments.get("max_entries", DEFAULT_DIR_ENTRIES)), MAX_DIR_ENTRIES)
        recursive = bool(arguments.get("recursive", False))
        max_depth = min(int(arguments.get("max_depth", 3)), MAX_DIR_DEPTH)

        target = _resolve_and_validate_path(root, req_path)

        if not target.exists():
            raise NotFoundError(f"directory not found: {req_path!r}")
        if not target.is_dir():
            raise ValueError(f"path {req_path!r} is not a directory")

        resolved_root = root.resolve()
        entries, truncated = await asyncio.to_thread(
            _scan_dir_sync,
            target,
            resolved_root,
            max_entries,
            recursive,
            max_depth,
        )

        return {
            "path": req_path,
            "entries": entries,
            "total_entries": len(entries),
            "truncated": truncated,
        }

    return Tool(
        ToolDefinition(
            name="starter.directory_list",
            version="1.0.0",
            description="List directory contents strictly contained within the workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "default": "",
                        "description": "Relative directory path within workspace.",
                    },
                    "recursive": {
                        "type": "boolean",
                        "default": False,
                        "description": "Whether to list subdirectories recursively.",
                    },
                    "max_entries": {
                        "type": "integer",
                        "default": DEFAULT_DIR_ENTRIES,
                        "description": "Maximum entries to return (up to 1000).",
                    },
                    "max_depth": {
                        "type": "integer",
                        "default": 3,
                        "description": "Maximum directory recursion depth (up to 5).",
                    },
                },
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "entries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "path": {"type": "string"},
                                "type": {"type": "string", "enum": ["file", "directory", "other"]},
                                "size_bytes": {"type": "integer"},
                            },
                            "required": ["name", "path", "type", "size_bytes"],
                        },
                    },
                    "total_entries": {"type": "integer"},
                    "truncated": {"type": "boolean"},
                },
                "required": ["path", "entries", "total_entries", "truncated"],
            },
            required_permissions=frozenset({"fs.read"}),
            side_effect=SideEffect.READ,
            idempotency=Idempotency.IDEMPOTENT,
        ),
        execute,
    )
