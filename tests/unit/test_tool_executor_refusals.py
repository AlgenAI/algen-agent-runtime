"""Calls the executor refuses: approval-required tools without an approval, and refusals a
tool raises itself before its side effect."""

from __future__ import annotations

from typing import Any

import pytest

from algen_agent_runtime.exceptions.errors import PolicyDeniedError
from algen_agent_runtime.persistence.tool_executions import InMemoryToolExecutionStore
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.contracts import (
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionStatus,
)
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry


def _context(key: str) -> ToolContext:
    return ToolContext(
        run_id="run-1",
        step_id="step-1",
        tenant_id="tenant-a",
        user_id="alice",
        permissions=frozenset(),
        idempotency_key=key,
    )


def _executor(handler: Any, **definition: Any) -> tuple[ToolExecutor, InMemoryToolExecutionStore]:
    registry = ToolRegistry()
    registry.register(
        Tool(
            ToolDefinition(
                name="mail.send",
                version="1",
                description="Send an email",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                side_effect=SideEffect.EXTERNAL,
                **definition,
            ),
            handler,
        )
    )
    store = InMemoryToolExecutionStore()
    return ToolExecutor(registry, CompositePolicyEngine(), executions=store), store


async def test_an_approval_required_tool_is_refused_without_an_approval() -> None:
    sent: list[Any] = []
    executor, store = _executor(lambda a, c: sent.append(a) or {}, requires_approval=True)

    with pytest.raises(PolicyDeniedError, match="requires an approval"):
        await executor.execute("mail.send", {}, _context("k1"))

    assert sent == []
    # Refused before the ledger: nothing to reconcile, and nothing blocks a later approved call.
    assert await store.get("k1", "tenant-a") is None
    result = await executor.execute("mail.send", {}, _context("k1"), approval="tool_call:c1")
    assert result.value == {} and len(sent) == 1


async def test_a_refusal_the_tool_raises_is_recorded_not_attempted() -> None:
    """A recipient allowlist refuses before sending: the email was certainly not sent."""
    attempts: list[Any] = []

    def send(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        attempts.append(arguments)
        if len(attempts) == 1:
            raise PolicyDeniedError("recipient domain is not allowed")
        return {"sent": True}

    executor, store = _executor(send)

    with pytest.raises(PolicyDeniedError):
        await executor.execute("mail.send", {}, _context("k1"))

    record = await store.get("k1", "tenant-a")
    assert record is not None and record.status == ToolExecutionStatus.FAILED
    # Not indeterminate, so a retry runs instead of asking for manual reconciliation.
    assert (await executor.execute("mail.send", {}, _context("k1"))).value == {"sent": True}
