import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.skipif(shutil.which("bash") is None, reason="Release workflow runs in Bash")
def test_release_notes_remain_literal_and_tag_targets_tested_commit(tmp_path: Path) -> None:
    workflow_path = Path(__file__).parents[2] / ".github/workflows/auto-release.yml"
    workflow = yaml.safe_load(workflow_path.read_text())
    step = next(
        step
        for step in workflow["jobs"]["auto-release"]["steps"]
        if step.get("name") == "Create GitHub Release"
    )
    assert step["env"]["RELEASE_NOTES"] == "${{ steps.changelog.outputs.body }}"
    script = step["run"].replace("${{ steps.version.outputs.tag }}", "v0.1.0a7")
    script = script.replace("${{ steps.version.outputs.version }}", "0.1.0a7")
    assert "${{" not in script

    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s\\0" "$@" > "$CAPTURE_ARGS"\n')
    gh.chmod(0o755)
    marker = tmp_path / "executed"
    notes = f'Preserve `touch {marker}` and $(touch {marker})\n"quoted" $HOME\n'
    captured = tmp_path / "arguments"
    commit = "a" * 40
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "RELEASE_NOTES": notes,
        "GITHUB_SHA": commit,
        "CAPTURE_ARGS": str(captured),
    }
    subprocess.run(["bash", "-e", "-c", script], env=env, check=True, capture_output=True)
    args = captured.read_bytes().decode().split("\0")[:-1]
    assert args[:3] == ["release", "create", "v0.1.0a7"]
    assert "--prerelease" in args
    assert args[args.index("--target") + 1] == commit
    notes_path = Path(args[args.index("--notes-file") + 1])
    assert notes_path.read_text() == notes + "\n"
    assert not marker.exists()
