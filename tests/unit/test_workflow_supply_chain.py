"""The CI and release workflows keep the supply-chain rules from the 0.2.0b3 security assessment.

A moved action tag is how the 2026 LiteLLM compromise reached a PyPI token, so every action is
pinned to a full commit SHA, no checkout leaves its token behind, the release never restores a
cache and builds only with the hash-locked build tools, and no personal access token is used.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
_USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)", re.MULTILINE)
_PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _steps(workflow: dict) -> list[dict]:
    return [step for job in workflow.get("jobs", {}).values() for step in job.get("steps", [])]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_actions_are_pinned_to_commit_shas(path: Path) -> None:
    unpinned = [ref for ref in _USES.findall(path.read_text()) if not _PINNED.match(ref)]
    assert not unpinned, f"{path.name}: pin these to a full commit SHA: {unpinned}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_checkouts_do_not_persist_credentials(path: Path) -> None:
    workflow = yaml.safe_load(path.read_text())
    for step in _steps(workflow):
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            assert (step.get("with") or {}).get("persist-credentials") is False, path.name


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_no_personal_access_tokens(path: Path) -> None:
    secrets = set(re.findall(r"secrets\.([A-Z_]+)", path.read_text()))
    assert not secrets, f"{path.name} uses repository secrets {sorted(secrets)}; use GITHUB_TOKEN"


def test_release_builds_from_locked_tools_without_caches() -> None:
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    workflow = yaml.safe_load(text)
    assert "cache:" not in text
    build = workflow["jobs"]["build"]["steps"]
    runs = [step.get("run", "") for step in build]
    assert "python -m pip install --require-hashes --no-deps -r requirements/build.txt" in runs
    assert "python -m build --no-isolation" in runs
    # The job that builds the artifacts installs nothing else and runs no tests.
    assert not any("pytest" in run or "-e ." in run for run in runs)
    publish = workflow["jobs"]["publish"]
    assert publish["environment"] == "pypi"
    assert publish["permissions"]["id-token"] == "write"
    # Workflow inputs reach shell scripts only through the environment.
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            assert "${{" not in step.get("run", ""), step.get("run")


def test_locked_requirements_are_hash_pinned() -> None:
    for name in ("build.txt", "ci.txt", "service.txt", "security.txt"):
        lines = (ROOT / "requirements" / name).read_text().splitlines()
        pins = [line for line in lines if re.match(r"^[A-Za-z0-9]", line)]
        assert pins, name
        assert all("==" in line for line in pins), name
        assert all("--hash=sha256:" in line or line.endswith("\\") for line in pins), name


def test_dockerfile_pins_the_base_image_and_installs_locked_dependencies() -> None:
    text = (ROOT / "Dockerfile").read_text()
    for line in text.splitlines():
        if line.startswith("FROM "):
            assert "@sha256:" in line, line
    assert "--require-hashes" in text
    assert "--no-isolation" in text
