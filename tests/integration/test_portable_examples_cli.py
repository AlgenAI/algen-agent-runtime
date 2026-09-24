from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize(
    ("module_name", "extra_args"),
    [
        ("examples.quickstart_approval.app", ["--approve-all"]),
        ("examples.quickstart_approval.app", ["--reject-all"]),
        ("examples.quickstart_tool.app", []),
        ("examples.pattern_approval_workflow.app", ["--approve-all"]),
        ("examples.pattern_governed_research.app", []),
        ("examples.pattern_langgraph_governance.app", []),
        ("examples.pattern_multi_agent_fanout.app", []),
        ("examples.pattern_child_workflow.app", []),
        ("examples.pattern_evaluation_gate.app", []),
        (
            "examples.reference_customer_support.app",
            [
                "--answers-file",
                "examples/reference_customer_support/fixtures/automation-answers.json",
            ],
        ),
        ("examples.reference_incident_response.app", ["--approve-all"]),
        ("examples.reference_invoice_exceptions.app", ["--approve-all"]),
    ],
)
def test_portable_examples_terminate_non_interactively(
    module_name: str, extra_args: list[str]
) -> None:
    cmd = [sys.executable, "-m", module_name, *extra_args]
    result = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,  # non-interactive
    )
    assert result.returncode == 0, (
        f"Command {cmd} failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


def test_quickstart_approval_with_answers_file(tmp_path: Path) -> None:
    answers_file = tmp_path / "answers.json"
    answers_file.write_text(
        json.dumps([{"type": "approval", "decision": "approved"}]),
        encoding="utf-8",
    )
    cmd = [
        sys.executable,
        "-m",
        "examples.quickstart_approval.app",
        "--answers-file",
        str(answers_file),
    ]
    result = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert '"decision": "approved"' in result.stdout


def test_customer_support_clarification_and_approval_with_answers_file(tmp_path: Path) -> None:
    answers_file = tmp_path / "customer_answers.json"
    answers_file.write_text(
        json.dumps(
            [
                {"type": "clarification", "answer": "ORD-1234"},
                {"type": "approval", "decision": "approved"},
            ]
        ),
        encoding="utf-8",
    )
    cmd = [
        sys.executable,
        "-m",
        "examples.reference_customer_support.app",
        "--answers-file",
        str(answers_file),
    ]
    result = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert '"decision": "approved"' in result.stdout


def test_approval_without_policy_in_non_interactive_env_fails_fast() -> None:
    cmd = [sys.executable, "-m", "examples.quickstart_approval.app"]
    result = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode != 0
    assert "non-interactive environment without an automated decision policy" in result.stderr
