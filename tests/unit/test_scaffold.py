from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

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

    # The generated plugin declares the tool the agent enables, so the project passes check
    check = subprocess.run(
        [sys.executable, "-m", "algen_agent_runtime.cli", "check", "agent.yaml", "--path", "."],
        cwd=str(dest),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert check.returncode == 0, f"STDOUT:\n{check.stdout}\nSTDERR:\n{check.stderr}"
    assert (dest / "my_agent_plugin.py").exists()


def test_scaffold_approval_template(tmp_path: Path) -> None:
    dest = tmp_path / "my-approval"
    scaffold_project("my-approval", dest, template="approval")

    assert (dest / "agent.yaml").exists()
    assert (dest / f"{dest.name.replace('-', '_')}_hooks.py").exists()
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
    assert (dest / f"{dest.name.replace('-', '_')}_hooks.py").exists()
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


def test_workflow_scaffold_renders_real_values(tmp_path: Path) -> None:
    dest = tmp_path / "test-workflow-render"
    scaffold_project("test-workflow-render", dest, template="workflow")

    result = subprocess.run(
        [sys.executable, str(dest / "app.py"), "Analyze performance"],
        cwd=str(dest),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "{{" not in result.stdout


def test_scaffolded_configs_enable_admission_control(tmp_path: Path) -> None:
    for template in AVAILABLE_TEMPLATES:
        dest = tmp_path / f"test-{template}"
        scaffold_project(f"test-{template}", dest, template=template)
        settings = load_settings((dest / "agent.yaml",))
        assert settings.api.rate_limiting.enabled is True


def _run_app(
    dest: Path, *arguments: str, stdin: str | None = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(dest / "app.py"), *arguments],
        cwd=str(dest),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_the_approval_app_waits_for_a_person_and_fails_closed(tmp_path: Path) -> None:
    dest = tmp_path / "gated"
    scaffold_project("gated", dest, template="approval")

    # No answer (closed input): nothing is applied and the exit status says so.
    unanswered = _run_app(dest, stdin="")
    assert unanswered.returncode == 2, unanswered.stderr
    assert "No decision" in unanswered.stderr
    assert '"result"' not in unanswered.stdout

    asked = _run_app(dest, stdin="approve\n")
    assert asked.returncode == 0, asked.stderr
    assert "Approve or reject?" in asked.stdout and '"status": "applied"' in asked.stdout

    rejected = _run_app(dest, stdin="reject\n")
    assert rejected.returncode == 0 and '"result"' not in rejected.stdout


def test_the_approval_app_decides_headless_only_when_told(tmp_path: Path) -> None:
    dest = tmp_path / "headless"
    scaffold_project("headless", dest, template="approval")

    approved = _run_app(dest, "--approve-all", stdin=None)
    assert approved.returncode == 0, approved.stderr
    assert '"status": "applied"' in approved.stdout
    rejected = _run_app(dest, "--reject-all", stdin=None)
    assert rejected.returncode == 0 and '"result"' not in rejected.stdout
    both = _run_app(dest, "--approve-all", "--reject-all", stdin=None)
    assert both.returncode == 2 and "not allowed with" in both.stderr


HEADERS = {"x-tenant-id": "acme", "x-user-id": "ops", "x-scopes": "runs:read runs:write"}


async def _until(client: Any, workflow_id: str, status: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for _ in range(500):
        found = (await client.get(f"/v1/workflows/runs/{workflow_id}", headers=HEADERS)).json()
        if found.get("status") == status:
            return found
        await asyncio.sleep(0.01)
    raise AssertionError(f"run never reached {status}: {found}")


@pytest.mark.parametrize("template", ["approval", "workflow"])
async def test_a_generated_workflow_runs_over_http(
    template: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deployment path: the service loads the trusted hook module, the submitted text
    reaches the approval and the model, and the run's result is its output."""
    httpx = pytest.importorskip("httpx")
    from algen_agent_runtime.api.app import create_app

    name = f"served-{template}"
    dest = tmp_path / name
    scaffold_project(name, dest, template=template)
    monkeypatch.syspath_prepend(str(dest))
    settings = load_settings((dest / "agent.yaml",))
    # The generated config limits request rates; this test polls faster than a client would.
    api = settings.api.model_copy(
        update={"rate_limiting": settings.api.rate_limiting.model_copy(update={"enabled": False})}
    )
    app = create_app(settings.model_copy(update={"api": api}))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        [entry] = (await client.get("/v1/workflows", headers=HEADERS)).json()["workflows"]
        assert entry["available"] is True, entry
        runs = f"/v1/workflows/{entry['name']}/runs"
        text = "Reserve two sample widgets"

        if template == "workflow":
            started = await client.post(
                runs, json={"input": text, "workflow_id": "workflow-run-1"}, headers=HEADERS
            )
            assert started.status_code == 202, started.text
            done = await _until(client, "workflow-run-1", "completed")
            assert done["output"]["status"] == "completed"
            assert text in done["output"]["analysis"]
            return

        started = await client.post(
            runs, json={"input": text, "workflow_id": "approval-run-1"}, headers=HEADERS
        )
        assert started.status_code == 202, started.text
        waiting = await _until(client, "approval-run-1", "awaiting_approval")
        assert waiting["pause"]["review"] == {"proposal.change": text}

        approval = "/v1/workflows/runs/approval-run-1/approval"
        invalid = await client.post(
            approval,
            json={"decision": "modified", "modified_parameters": {"change": ""}},
            headers=HEADERS,
        )
        assert invalid.status_code == 400, invalid.text
        assert "non-empty" in invalid.json()["detail"]
        assert (await _until(client, "approval-run-1", "awaiting_approval"))["pause"] == waiting[
            "pause"
        ]

        decided = await client.post(approval, json={"decision": "approved"}, headers=HEADERS)
        assert decided.status_code == 202, decided.text
        done = await _until(client, "approval-run-1", "completed")
        assert done["output"]["status"] == "applied"
        assert done["output"]["parameters"] == {"change": text}

        # A rejected change completes without being applied, so it has no output.
        await client.post(
            runs, json={"input": text, "workflow_id": "approval-run-2"}, headers=HEADERS
        )
        await _until(client, "approval-run-2", "awaiting_approval")
        await client.post(
            "/v1/workflows/runs/approval-run-2/approval",
            json={"decision": "rejected"},
            headers=HEADERS,
        )
        assert (await _until(client, "approval-run-2", "completed"))["output"] is None
