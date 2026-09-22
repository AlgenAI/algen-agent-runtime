from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from algen_agent_runtime.cli import build_parser, main
from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.scaffold import AVAILABLE_TEMPLATES, scaffold_project


def test_available_templates() -> None:
    assert "agent" in AVAILABLE_TEMPLATES
    assert "approval" in AVAILABLE_TEMPLATES
    assert "workflow" in AVAILABLE_TEMPLATES


def test_scaffold_default_agent_template(tmp_path: Path) -> None:
    dest = tmp_path / "my-agent"
    scaffold_project("my-agent", dest, template="agent")

    assert (dest / "agent.yaml").exists()
    assert (dest / "app.py").exists()
    assert (dest / "test_agent.py").exists()
    assert (dest / "README.md").exists()
    assert (dest / ".gitignore").exists()
    assert (dest / "traccia.toml.example").exists()

    # Load and validate settings
    settings = load_settings((dest / "agent.yaml",))
    assert "mock" in settings.providers
    assert any(a.name == "my-agent" for a in settings.agents)

    # Check zero literal secrets
    yaml_text = (dest / "agent.yaml").read_text(encoding="utf-8")
    assert "api_key" not in yaml_text or "env://" in yaml_text

    # Run generated test using pytest in subprocess
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(dest / "test_agent.py"),
        ],
        cwd=str(dest),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"


def test_scaffold_approval_template(tmp_path: Path) -> None:
    dest = tmp_path / "my-approval"
    scaffold_project("my-approval", dest, template="approval")

    assert (dest / "agent.yaml").exists()
    assert (dest / "hooks.py").exists()
    assert (dest / "app.py").exists()
    assert (dest / "test_agent.py").exists()

    settings = load_settings((dest / "agent.yaml",))
    assert "my-approval-workflow" in settings.workflows

    # Run generated tests
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(dest / "test_agent.py"),
        ],
        cwd=str(dest),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"


def test_scaffold_workflow_template(tmp_path: Path) -> None:
    dest = tmp_path / "my-dag"
    scaffold_project("my-dag", dest, template="workflow")

    assert (dest / "agent.yaml").exists()
    assert (dest / "hooks.py").exists()
    assert (dest / "app.py").exists()
    assert (dest / "test_agent.py").exists()

    settings = load_settings((dest / "agent.yaml",))
    assert "my-dag-dag" in settings.workflows

    # Run generated tests
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(dest / "test_agent.py"),
        ],
        cwd=str(dest),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"


def test_scaffold_refuses_non_empty_target_directory(tmp_path: Path) -> None:
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "preexisting.txt").write_text("existing content", encoding="utf-8")

    with pytest.raises(FileExistsError, match="not empty"):
        scaffold_project("test", dest)

    # With force=True, it succeeds and overwrites/adds files
    scaffold_project("test", dest, force=True)
    assert (dest / "agent.yaml").exists()


def test_scaffold_invalid_template(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown template"):
        scaffold_project("test", tmp_path / "bad", template="invalid-template")  # type: ignore[arg-type]


def test_cli_new_command(tmp_path: Path) -> None:
    dest = tmp_path / "cli-agent"
    main(["new", "cli-agent", "--dir", str(dest)])
    assert (dest / "agent.yaml").exists()
    assert (dest / "app.py").exists()


def test_cli_parser_options() -> None:
    parser = build_parser()
    args_serve = parser.parse_args(["serve", "--port", "8080", "--host", "127.0.0.1"])
    assert args_serve.command == "serve"
    assert args_serve.port == 8080
    assert args_serve.host == "127.0.0.1"

    args_new = parser.parse_args(["new", "my-app", "--template", "approval", "--force"])
    assert args_new.command == "new"
    assert args_new.name == "my-app"
    assert args_new.template == "approval"
    assert args_new.force is True
