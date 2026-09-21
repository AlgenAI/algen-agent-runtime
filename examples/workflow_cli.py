"""Small CLI host shared by the portable workflow examples.

The examples intentionally use the same ``WorkflowManifest`` and hook provider that Studio
loads.  This module is only a host: scheduling, fan-out, repair, predicates, joins, and pause
limits remain owned by :mod:`algen_agent_runtime.workflows`.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from pathlib import Path
from typing import Any

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.workflows import (
    MultiAgentWorkflowExecutor,
    WorkflowRegistry,
    WorkflowStatus,
)


async def run_example(config_path: Path, workflow_name: str, question: str) -> dict[str, Any]:
    """Execute one configured workflow, prompting on Runtime human checkpoints."""
    settings = load_settings((config_path,))
    manifest = settings.workflows[workflow_name]
    container = build_container(settings)
    try:
        registry = WorkflowRegistry()
        for configured in settings.workflows.values():
            if configured.hook_provider is None:
                raise RuntimeError(f"workflow {configured.name!r} does not declare a hook provider")
            module_name, factory_name = configured.hook_provider.split(":", 1)
            factory = getattr(importlib.import_module(module_name), factory_name)
            provided = factory(
                container=container,
                manifest=configured,
                environment={},
                tenant_id="example-tenant",
                user_id="example-user",
                project_id=workflow_name,
            )
            if inspect.isawaitable(provided):
                provided = await provided
            registry.register(configured, getattr(provided, "hooks", provided))
        hooks = registry.resolve(manifest.name, manifest.version)[1]
        values: dict[str, Any] = {
            "input": question,
            "question": question,
            "clarifications": [],
        }
        executor = MultiAgentWorkflowExecutor(
            AlgenAgentRuntimeClient(container.runtime),
            hooks,
            store=container.workflow_checkpoints,
            workflow_registry=registry,
        )
        state = await executor.run(
            manifest,
            values,
            tenant_id="example-tenant",
            user_id="example-user",
        )
        while state.status in {
            WorkflowStatus.AWAITING_INPUT,
            WorkflowStatus.AWAITING_APPROVAL,
        }:
            pause = state.pause or {}
            if state.status is WorkflowStatus.AWAITING_APPROVAL:
                prompt = str(pause.get("prompt") or "Approval required")
                answer = (
                    (await asyncio.to_thread(input, f"{prompt}\nApprove or reject? "))
                    .strip()
                    .lower()
                )
                decision = "approved" if answer in {"approve", "approved", "yes"} else "rejected"
                state = await executor.decide_approval(
                    manifest,
                    state.id,
                    tenant_id="example-tenant",
                    user_id="example-user",
                    decision=decision,
                )
                continue
            prompt = str(pause.get("question") or "Clarification required")
            answer = await asyncio.to_thread(input, f"{prompt}\n> ")
            values["clarifications"].append((prompt, answer))
            state = await executor.resume(
                manifest,
                state.id,
                tenant_id="example-tenant",
                values={"clarifications": values["clarifications"]},
            )
        if state.status != WorkflowStatus.COMPLETED:
            raise RuntimeError(state.error or f"workflow ended with {state.status.value}")
        return state.values
    finally:
        container.close()


def print_result(values: dict[str, Any]) -> None:
    """Print stable, inspectable output for shell users and smoke tests."""
    print(json.dumps(values, indent=2, default=str, sort_keys=True))
