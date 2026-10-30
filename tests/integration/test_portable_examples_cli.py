"""Decision inputs of the workflow examples' CLI; test_all_examples_run.py runs every example."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


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
