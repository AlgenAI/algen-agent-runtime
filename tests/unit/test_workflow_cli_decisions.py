from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from examples.workflow_cli import (
    AnswersFileDecisionProvider,
    ApproveAllDecisionProvider,
    InteractiveDecisionProvider,
    RejectAllDecisionProvider,
    add_decision_arguments,
    decision_provider_from_args,
    run_example,
)

ROOT = Path(__file__).parents[2]
APPROVAL_CONFIG = ROOT / "examples" / "quickstart_approval" / "agent.yaml"


@pytest.mark.asyncio
async def test_approve_all_decision_provider() -> None:
    provider = ApproveAllDecisionProvider()
    assert await provider.decide_approval("Approve action?", {}) == "approved"
    with pytest.raises(RuntimeError, match="--approve-all only resolves approvals"):
        await provider.provide_clarification("Need input", {})


@pytest.mark.asyncio
async def test_reject_all_decision_provider() -> None:
    provider = RejectAllDecisionProvider()
    assert await provider.decide_approval("Approve action?", {}) == "rejected"
    with pytest.raises(RuntimeError, match="--reject-all only resolves approvals"):
        await provider.provide_clarification("Need input", {})


def test_answers_file_missing_file_raises_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Answers file not found"):
        AnswersFileDecisionProvider(tmp_path / "nonexistent.json")


def test_answers_file_invalid_json_raises_error(tmp_path: Path) -> None:
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid JSON"):
        AnswersFileDecisionProvider(bad_json)


def test_answers_file_invalid_root_type(tmp_path: Path) -> None:
    invalid_root = tmp_path / "invalid_root.json"
    invalid_root.write_text('"just a string"', encoding="utf-8")
    with pytest.raises(ValueError, match="root must be a list"):
        AnswersFileDecisionProvider(invalid_root)


def test_answers_file_invalid_schema_entry(tmp_path: Path) -> None:
    bad_schema = tmp_path / "bad_schema.json"
    bad_schema.write_text(
        json.dumps([{"type": "approval", "extra_unsupported_key": True}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Schema validation failed"):
        AnswersFileDecisionProvider(bad_schema)


@pytest.mark.asyncio
async def test_answers_file_ordered_decisions(tmp_path: Path) -> None:
    answers_path = tmp_path / "answers.json"
    answers_path.write_text(
        json.dumps(
            [
                {"type": "approval", "decision": "approved"},
                {"type": "clarification", "answer": "Synthetically verified"},
                "rejected",
            ]
        ),
        encoding="utf-8",
    )
    provider = AnswersFileDecisionProvider(answers_path)
    assert await provider.decide_approval("Step 1", {}) == "approved"
    assert await provider.provide_clarification("Step 2", {}) == "Synthetically verified"
    assert await provider.decide_approval("Step 3", {}) == "rejected"

    # Exhausted answers
    with pytest.raises(RuntimeError, match="exhausted"):
        await provider.decide_approval("Step 4", {})


@pytest.mark.asyncio
async def test_answers_file_type_mismatch(tmp_path: Path) -> None:
    answers_path = tmp_path / "mismatch.json"
    answers_path.write_text(
        json.dumps([{"type": "clarification", "answer": "text only"}]),
        encoding="utf-8",
    )
    provider = AnswersFileDecisionProvider(answers_path)
    with pytest.raises(RuntimeError, match="expected approval, got clarification"):
        await provider.decide_approval("Step 1", {})


@pytest.mark.asyncio
async def test_interactive_decision_provider_non_tty_fails_fast() -> None:
    provider = InteractiveDecisionProvider(is_tty=False)
    with pytest.raises(
        RuntimeError, match="non-interactive environment without an automated decision policy"
    ):
        await provider.decide_approval("Approve?", {})

    with pytest.raises(
        RuntimeError, match="non-interactive environment without an automated decision policy"
    ):
        await provider.provide_clarification("Clarify?", {})


@pytest.mark.asyncio
async def test_interactive_decision_provider_eof_error() -> None:
    provider = InteractiveDecisionProvider(is_tty=True)
    with patch("builtins.input", side_effect=EOFError("unexpected EOF")):
        with pytest.raises(
            RuntimeError, match="End of input reached on stdin while awaiting approval"
        ):
            await provider.decide_approval("Approve?", {})

    with patch("builtins.input", side_effect=EOFError("unexpected EOF")):
        with pytest.raises(
            RuntimeError, match="End of input reached on stdin while awaiting clarification"
        ):
            await provider.provide_clarification("Clarify?", {})


def test_decision_arguments_and_from_args(tmp_path: Path) -> None:
    parser = argparse.ArgumentParser()
    add_decision_arguments(parser)

    # --approve-all
    args = parser.parse_args(["--approve-all"])
    assert isinstance(decision_provider_from_args(args), ApproveAllDecisionProvider)

    # --reject-all
    args = parser.parse_args(["--reject-all"])
    assert isinstance(decision_provider_from_args(args), RejectAllDecisionProvider)

    # --answers-file
    p = tmp_path / "answers.json"
    p.write_text("[]", encoding="utf-8")
    args = parser.parse_args(["--answers-file", str(p)])
    assert isinstance(decision_provider_from_args(args), AnswersFileDecisionProvider)

    # Conflicting arguments
    args_conflict = parser.parse_args(["--approve-all", "--reject-all"])
    with pytest.raises(ValueError, match="Cannot specify both"):
        decision_provider_from_args(args_conflict)

    # Default
    args_default = parser.parse_args([])
    assert isinstance(decision_provider_from_args(args_default), InteractiveDecisionProvider)


@pytest.mark.asyncio
async def test_run_example_with_approve_all() -> None:
    values = await run_example(
        APPROVAL_CONFIG,
        "approval-quickstart",
        "Enable credit for test account",
        approve_all=True,
    )
    assert values["approval"]["decision"] == "approved"
    assert values["result"]["status"] == "applied"


@pytest.mark.asyncio
async def test_run_example_with_reject_all() -> None:
    values = await run_example(
        APPROVAL_CONFIG,
        "approval-quickstart",
        "Enable credit for test account",
        reject_all=True,
    )
    assert values["approval"]["decision"] == "rejected"
    assert "result" not in values


@pytest.mark.asyncio
async def test_run_example_with_answers_file(tmp_path: Path) -> None:
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps([{"type": "approval", "decision": "approved"}]), encoding="utf-8")
    values = await run_example(
        APPROVAL_CONFIG,
        "approval-quickstart",
        "Enable credit for test account",
        answers_file=answers,
    )
    assert values["approval"]["decision"] == "approved"


@pytest.mark.asyncio
async def test_run_example_non_tty_without_decision_policy_fails() -> None:
    # Simulates non-TTY runner (CI, pipe, background process) without decision policy
    provider = InteractiveDecisionProvider(is_tty=False)
    with pytest.raises(RuntimeError, match="non-interactive environment"):
        await run_example(
            APPROVAL_CONFIG,
            "approval-quickstart",
            "Enable credit for test account",
            decision_provider=provider,
        )
