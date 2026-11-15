import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).parents[2] / ".github/workflows/auto-release.yml"


def _step(name: str) -> dict:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return next(
        step for step in workflow["jobs"]["auto-release"]["steps"] if step.get("name") == name
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="Release workflow runs in Bash")
def test_release_notes_remain_literal_and_tag_targets_tested_commit(tmp_path: Path) -> None:
    extract, create = _step("Extract changelog"), _step("Create GitHub Release")
    # Expressions reach the scripts only through the environment.
    assert "${{" not in extract["run"] and "${{" not in create["run"]

    marker = tmp_path / "executed"
    notes = f'Preserve `touch {marker}` and $(touch {marker})\n"quoted" $HOME'
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [0.2.0b9] - 2026-12-01\n\n{notes}\n\n## [0.2.0b8]\n\nolder\n"
    )
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s\\0" "$@" > "$CAPTURE_ARGS"\n')
    gh.chmod(0o755)
    captured = tmp_path / "arguments"
    commit = "a" * 40
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_SHA": commit,
        "CAPTURE_ARGS": str(captured),
        "VERSION": "0.2.0b9",
        "TAG": "v0.2.0b9",
    }
    for step in (extract, create):
        subprocess.run(
            ["bash", "-e", "-c", step["run"]],
            env=env,
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
    args = captured.read_bytes().decode().split("\0")[:-1]
    assert args[:3] == ["release", "create", "v0.2.0b9"]
    assert "--prerelease" in args
    assert args[args.index("--target") + 1] == commit
    notes_path = Path(args[args.index("--notes-file") + 1])
    assert notes_path.read_text() == notes + "\n"
    assert not marker.exists()


def test_release_is_started_with_the_workflow_token() -> None:
    start = _step("Start release.yml")
    assert start["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "gh workflow run release.yml" in start["run"]
    assert "secrets." not in WORKFLOW.read_text()
