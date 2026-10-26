"""Helpers for testing tools offline: a fake context and the tool contract check."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from algen_agent_runtime.tools.contracts import Idempotency, SideEffect, Tool, ToolContext

WRITE_EFFECTS = frozenset({SideEffect.WRITE, SideEffect.EXTERNAL, SideEffect.DESTRUCTIVE})


def fake_tool_context(
    tool: Tool | None = None,
    *,
    permissions: Iterable[str] | None = None,
    tenant_id: str = "test-tenant",
    user_id: str = "test-user",
    run_id: str = "test-run",
    step_id: str = "test-step",
    idempotency_key: str | None = None,
    secrets: Mapping[str, str] | None = None,
) -> ToolContext:
    """A :class:`ToolContext` for calling a tool directly in a test.

    With ``tool`` and no ``permissions``, the context holds exactly the permissions the tool
    requires. Each call gets a fresh idempotency key unless one is given.
    """
    granted = (
        frozenset(permissions)
        if permissions is not None
        else (tool.definition.required_permissions if tool is not None else frozenset())
    )
    return ToolContext(
        run_id=run_id,
        step_id=step_id,
        tenant_id=tenant_id,
        user_id=user_id,
        permissions=granted,
        idempotency_key=idempotency_key or f"{run_id}:{uuid.uuid4().hex}",
        secrets=dict(secrets or {}),
    )


def _open_objects(schema: Any, path: str) -> list[str]:
    found: list[str] = []
    if isinstance(schema, dict):
        if (
            schema.get("type") == "object"
            and "properties" in schema
            and schema.get("additionalProperties") is not False
        ):
            found.append(path or "/")
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                for name, nested in value.items():
                    found.extend(_open_objects(nested, f"{path}/{name}"))
            elif key != "additionalProperties" or isinstance(value, dict):
                found.extend(_open_objects(value, path))
    elif isinstance(schema, list):
        for item in schema:
            found.extend(_open_objects(item, path))
    return found


def tool_contract_problems(
    tool: Tool, *, longest_wait_seconds: float | None = None
) -> tuple[str, ...]:
    """Everything that breaks the Runtime's tool contract; empty when the tool is sound.

    A tool needs a description, an object input schema that closes every object
    (``additionalProperties: false``) and an output schema. A tool that writes, sends or
    deletes needs a permission and an idempotency key (``keyed``, or ``idempotent`` when a repeat
    is harmless) and, unless idempotent, a single attempt. ``longest_wait_seconds`` is the
    longest the tool may legitimately wait, which its timeout must exceed.
    """
    definition = tool.definition
    problems: list[str] = []
    if not definition.description.strip():
        problems.append("the description is empty")
    if definition.input_schema.get("type") != "object":
        problems.append("the input schema is not an object")
    problems.extend(
        f"the input schema leaves {path} open; set additionalProperties: false"
        for path in _open_objects(definition.input_schema, "")
    )
    if not definition.output_schema:
        problems.append("the output schema is empty")
    if definition.side_effect in WRITE_EFFECTS:
        effect = definition.side_effect.value
        if not definition.required_permissions:
            problems.append(f"a {effect} tool requires no permission")
        if definition.idempotency == Idempotency.NON_IDEMPOTENT:
            problems.append(f"a {effect} tool must be keyed by the idempotency key")
        if (
            definition.idempotency != Idempotency.IDEMPOTENT
            and definition.retry_policy.max_attempts > 1
        ):
            problems.append(
                f"a {effect} tool is retried {definition.retry_policy.max_attempts} times; "
                "a retry after a timeout could repeat the change, so allow one attempt"
            )
    if longest_wait_seconds is not None and definition.timeout_seconds <= longest_wait_seconds:
        problems.append(
            f"the timeout ({definition.timeout_seconds}s) does not exceed the longest wait "
            f"({longest_wait_seconds}s)"
        )
    return tuple(problems)


def assert_tool_contract(tool: Tool, *, longest_wait_seconds: float | None = None) -> None:
    """Fail with every contract problem :func:`tool_contract_problems` finds."""
    problems = tool_contract_problems(tool, longest_wait_seconds=longest_wait_seconds)
    if problems:
        raise AssertionError(
            f"tool {tool.definition.name!r} breaks the tool contract:\n- " + "\n- ".join(problems)
        )
