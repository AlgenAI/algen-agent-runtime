"""Small CLI host shared by the portable workflow examples.

The examples intentionally use the same ``WorkflowManifest`` and hook provider that Studio
loads. This module is only a host: scheduling, fan-out, repair, predicates, joins, and pause
limits remain owned by :mod:`algen_agent_runtime.workflows`.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.workflows import (
    MultiAgentWorkflowExecutor,
    WorkflowRegistry,
    WorkflowStatus,
)


class DecisionProvider(Protocol):
    """Protocol for resolving human checkpoints (approvals and clarifications)."""

    async def decide_approval(self, prompt: str, details: dict[str, Any]) -> str:
        """Return 'approved' or 'rejected'."""
        ...

    async def provide_clarification(self, prompt: str, details: dict[str, Any]) -> str:
        """Return clarification answer text."""
        ...


class ApproveAllDecisionProvider:
    """Deterministic policy that approves every approval checkpoint."""

    async def decide_approval(self, prompt: str, details: dict[str, Any]) -> str:
        return "approved"

    async def provide_clarification(self, prompt: str, details: dict[str, Any]) -> str:
        raise RuntimeError(
            f"Workflow paused for clarification ({prompt!r}) but --approve-all only resolves approvals. "
            "Pass --answers-file PATH to provide clarification responses."
        )


class RejectAllDecisionProvider:
    """Deterministic policy that rejects every approval checkpoint."""

    async def decide_approval(self, prompt: str, details: dict[str, Any]) -> str:
        return "rejected"

    async def provide_clarification(self, prompt: str, details: dict[str, Any]) -> str:
        raise RuntimeError(
            f"Workflow paused for clarification ({prompt!r}) but --reject-all only resolves approvals. "
            "Pass --answers-file PATH to provide clarification responses."
        )


class AnswerItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["approval", "clarification"] | None = None
    decision: Literal["approved", "rejected"] | None = None
    answer: str | None = None


class AnswersFileDecisionProvider:
    """Loads ordered approval/clarification decisions from a JSON file with schema validation."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if not self._path.exists():
            raise FileNotFoundError(f"Answers file not found: {self._path}")
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid JSON in answers file {self._path}: {exc}") from exc

        items: list[Any]
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict) and "answers" in raw and isinstance(raw["answers"], list):
            items = raw["answers"]
        else:
            raise ValueError(
                f"Invalid answers file structure in {self._path}: root must be a list of answers "
                "or an object with an 'answers' list"
            )

        self._answers: list[AnswerItem | str] = []
        for idx, item in enumerate(items):
            if isinstance(item, str):
                self._answers.append(item)
            elif isinstance(item, dict):
                try:
                    self._answers.append(AnswerItem.model_validate(item))
                except Exception as exc:
                    raise ValueError(
                        f"Schema validation failed for answer at index {idx} in {self._path}: {exc}"
                    ) from exc
            else:
                raise ValueError(
                    f"Invalid entry at index {idx} in {self._path}: must be an object or string, "
                    f"got {type(item).__name__}"
                )

    async def decide_approval(self, prompt: str, details: dict[str, Any]) -> str:
        if not self._answers:
            raise RuntimeError(
                f"Answers file {self._path} exhausted: workflow requested approval ({prompt!r}) "
                "but no more answers remain."
            )
        item = self._answers.pop(0)
        if isinstance(item, AnswerItem):
            if item.type == "clarification" and item.decision is None:
                raise RuntimeError(
                    f"Answers file {self._path} type mismatch: expected approval, got clarification "
                    f"answer {item.answer!r}"
                )
            if item.decision is not None:
                return item.decision
            if item.answer is not None:
                norm = item.answer.strip().lower()
                return "approved" if norm in {"approve", "approved", "yes", "true"} else "rejected"
            return "approved"
        norm = item.strip().lower()
        return "approved" if norm in {"approve", "approved", "yes", "true"} else "rejected"

    async def provide_clarification(self, prompt: str, details: dict[str, Any]) -> str:
        if not self._answers:
            raise RuntimeError(
                f"Answers file {self._path} exhausted: workflow requested clarification ({prompt!r}) "
                "but no more answers remain."
            )
        item = self._answers.pop(0)
        if isinstance(item, AnswerItem):
            if item.type == "approval" and item.answer is None:
                raise RuntimeError(
                    f"Answers file {self._path} type mismatch: expected clarification, got approval "
                    f"decision {item.decision!r}"
                )
            if item.answer is not None:
                return item.answer
            if item.decision is not None:
                return item.decision
            return ""
        return item


class InteractiveDecisionProvider:
    """Prompts human operators on standard input/output when running in an interactive TTY."""

    def __init__(self, is_tty: bool | None = None) -> None:
        self._is_tty = is_tty if is_tty is not None else sys.stdin.isatty()

    async def decide_approval(self, prompt: str, details: dict[str, Any]) -> str:
        if not self._is_tty:
            raise RuntimeError(
                f"Workflow paused for approval ({prompt!r}) in a non-interactive environment without "
                "an automated decision policy. Pass --approve-all, --reject-all, or --answers-file PATH."
            )
        try:
            answer = (
                (await asyncio.to_thread(input, f"{prompt}\nApprove or reject? "))
                .strip()
                .lower()
            )
            return "approved" if answer in {"approve", "approved", "yes"} else "rejected"
        except EOFError as exc:
            raise RuntimeError(
                f"End of input reached on stdin while awaiting approval ({prompt!r}). "
                "Pass --approve-all, --reject-all, or --answers-file PATH in non-interactive environments."
            ) from exc

    async def provide_clarification(self, prompt: str, details: dict[str, Any]) -> str:
        if not self._is_tty:
            raise RuntimeError(
                f"Workflow paused for clarification ({prompt!r}) in a non-interactive environment without "
                "an automated decision policy. Pass --answers-file PATH."
            )
        try:
            return await asyncio.to_thread(input, f"{prompt}\n> ")
        except EOFError as exc:
            raise RuntimeError(
                f"End of input reached on stdin while awaiting clarification ({prompt!r}). "
                "Pass --answers-file PATH in non-interactive environments."
            ) from exc


def add_decision_arguments(parser: argparse.ArgumentParser) -> None:
    """Add standard automation and decision flags to an example's CLI argument parser."""
    group = parser.add_argument_group("decision policy (automation and non-interactive execution)")
    group.add_argument(
        "--approve-all",
        action="store_true",
        help="Automatically approve all workflow approval checkpoints",
    )
    group.add_argument(
        "--reject-all",
        action="store_true",
        help="Automatically reject all workflow approval checkpoints",
    )
    group.add_argument(
        "--answers-file",
        type=Path,
        default=None,
        help="Path to JSON file containing ordered approval/clarification decisions",
    )


def decision_provider_from_args(args: argparse.Namespace) -> DecisionProvider:
    """Construct the appropriate decision provider from parsed CLI arguments."""
    approve_all = getattr(args, "approve_all", False)
    reject_all = getattr(args, "reject_all", False)
    answers_file = getattr(args, "answers_file", None)

    if approve_all and reject_all:
        raise ValueError("Cannot specify both --approve-all and --reject-all")
    if approve_all:
        return ApproveAllDecisionProvider()
    if reject_all:
        return RejectAllDecisionProvider()
    if answers_file is not None:
        return AnswersFileDecisionProvider(answers_file)
    return InteractiveDecisionProvider()


async def run_example(
    config_path: Path,
    workflow_name: str,
    question: str,
    *,
    decision_provider: DecisionProvider | None = None,
    approve_all: bool = False,
    reject_all: bool = False,
    answers_file: str | Path | None = None,
) -> dict[str, Any]:
    """Execute one configured workflow, resolving human checkpoints via decision_provider."""
    if decision_provider is None:
        if approve_all and reject_all:
            raise ValueError("Cannot specify both approve_all and reject_all")
        if approve_all:
            decision_provider = ApproveAllDecisionProvider()
        elif reject_all:
            decision_provider = RejectAllDecisionProvider()
        elif answers_file is not None:
            decision_provider = AnswersFileDecisionProvider(answers_file)
        else:
            decision_provider = InteractiveDecisionProvider()

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
                decision = await decision_provider.decide_approval(prompt, pause)
                state = await executor.decide_approval(
                    manifest,
                    state.id,
                    tenant_id="example-tenant",
                    user_id="example-user",
                    decision=decision,
                )
                continue
            prompt = str(pause.get("question") or "Clarification required")
            answer = await decision_provider.provide_clarification(prompt, pause)
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
