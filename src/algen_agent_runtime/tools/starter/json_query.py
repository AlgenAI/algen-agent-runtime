from __future__ import annotations

import json
import re
from typing import Any

from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)

MAX_INPUT_BYTES = 1_048_576  # 1 MB
MAX_OUTPUT_BYTES = 262_144  # 256 KB
MAX_PATH_LENGTH = 256
MAX_PATH_DEPTH = 32

_PATH_TOKEN_RE = re.compile(r"([^\.\[\]]+)|\[(\d+|\*)\]")


def _parse_path(path: str) -> list[str | int]:
    if len(path) > MAX_PATH_LENGTH:
        raise ValueError(f"path length exceeds maximum limit of {MAX_PATH_LENGTH}")
    tokens: list[str | int] = []
    for match in _PATH_TOKEN_RE.finditer(path):
        key, idx = match.groups()
        if key is not None:
            tokens.append(key)
        elif idx is not None:
            if idx == "*":
                tokens.append("*")
            else:
                tokens.append(int(idx))
    if len(tokens) > MAX_PATH_DEPTH:
        raise ValueError(f"path depth exceeds maximum limit of {MAX_PATH_DEPTH}")
    return tokens


def _query_data(data: Any, tokens: list[str | int]) -> Any:
    current = data
    for i, token in enumerate(tokens):
        if token == "*":
            # Wildcard projection over list or dict
            remaining = tokens[i + 1 :]
            if isinstance(current, list):
                return [_query_data(item, remaining) for item in current]
            elif isinstance(current, dict):
                return [_query_data(val, remaining) for val in current.values()]
            else:
                raise ValueError("wildcard '*' operator requires a list or dict target")
        elif isinstance(token, int):
            if not isinstance(current, list):
                raise ValueError(f"cannot index non-list object with integer {token}")
            if token < 0 or token >= len(current):
                raise IndexError(f"index {token} out of range (list size: {len(current)})")
            current = current[token]
        else:
            if not isinstance(current, dict):
                raise ValueError(f"cannot access key {token!r} on non-dict object")
            if token not in current:
                raise KeyError(f"key {token!r} not found")
            current = current[token]
    return current


def execute_json_query(
    data: Any,
    path: str = "",
    operation: str = "get",
) -> Any:
    """Execute query and transform operation on JSON structure."""
    if isinstance(data, str):
        if len(data.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError(f"input data exceeds maximum size limit of {MAX_INPUT_BYTES} bytes")
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed JSON input: {exc.msg}") from exc
    else:
        parsed = data

    path = path.strip()
    if path:
        tokens = _parse_path(path)
        target = _query_data(parsed, tokens)
    else:
        target = parsed

    if operation == "get":
        result = target
    elif operation == "keys":
        if isinstance(target, dict):
            result = list(target.keys())
        else:
            raise ValueError(
                f"operation 'keys' is only valid for dict, got {type(target).__name__}"
            )
    elif operation == "length":
        if isinstance(target, (list, dict, str)):
            result = len(target)
        else:
            raise ValueError(
                f"operation 'length' requires list, dict, or string, got {type(target).__name__}"
            )
    else:
        raise ValueError(f"unsupported operation: {operation!r}")

    # Verify output size bound
    try:
        serialized = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"result is not JSON-serializable: {exc}") from exc

    if len(serialized.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ValueError(f"query result exceeds maximum output limit of {MAX_OUTPUT_BYTES} bytes")

    return result


def json_query_tool() -> Tool:
    """Create a safe starter JSON query and transform tool."""

    async def execute(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        data = arguments["data"]
        path = arguments.get("path", "")
        op = arguments.get("operation", "get")
        result = execute_json_query(data, path=path, operation=op)
        return {"result": result}

    return Tool(
        ToolDefinition(
            name="starter.json_query",
            version="1.0.0",
            description="Query, filter, and extract structured data from JSON objects or strings safely.",
            input_schema={
                "type": "object",
                "properties": {
                    "data": {
                        "description": "JSON object, list, or JSON-formatted string",
                    },
                    "path": {
                        "type": "string",
                        "description": "Path to navigate (e.g. 'users[0].name', 'metrics.*', 'info')",
                    },
                    "operation": {
                        "type": "string",
                        "enum": ["get", "keys", "length"],
                        "default": "get",
                        "description": "Operation to perform: 'get' (default), 'keys' (dict keys), 'length'",
                    },
                },
                "required": ["data"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "result": {},
                },
                "required": ["result"],
            },
            side_effect=SideEffect.NONE,
            idempotency=Idempotency.IDEMPOTENT,
        ),
        execute,
    )
