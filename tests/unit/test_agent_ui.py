from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.cli import build_parser, main
from algen_agent_runtime.config.settings import AppSettings, load_settings
from algen_agent_runtime.ui.generator import export_agent_ui_html, generate_agent_ui_html
from algen_agent_runtime.ui.spec import extract_agent_manifest_spec


def test_extract_manifest_spec_single_agent() -> None:
    settings = load_settings(("examples/quickstart_local_chat/agent.yaml",))
    spec = extract_agent_manifest_spec(settings)

    assert spec["title"]
    assert spec["stats"]["agent_count"] == 2
    assert spec["stats"]["workflow_count"] == 0
    assert spec["stats"]["provider_count"] == 2
    assert len(spec["agents"]) == 2

    # Check agent serialization
    agent_names = [a["name"] for a in spec["agents"]]
    assert "minimal" in agent_names
    assert "local-private" in agent_names

    minimal = next(a for a in spec["agents"] if a["name"] == "minimal")
    assert minimal["default_model"]["provider"] == "local"
    assert minimal["default_model"]["model"] == "llama3.2"
    assert "chat" in minimal["default_model"]["required_capabilities"]


def test_extract_manifest_spec_multi_agent_workflow() -> None:
    settings = load_settings(("examples/pattern_multi_agent_fanout/agent.yaml",))
    spec = extract_agent_manifest_spec(settings)

    assert spec["stats"]["agent_count"] == 3
    assert spec["stats"]["workflow_count"] == 1
    assert "multi-agent-fanout" in spec["workflows"]

    wf = spec["workflows"]["multi-agent-fanout"]
    assert wf["name"] == "multi-agent-fanout"
    assert len(wf["nodes"]) == 4

    # Verify DAG calculation
    dag = wf["dag"]
    assert "columns" in dag
    assert "levels" in dag
    assert "edges" in dag

    # Check plan -> research -> evidence -> synthesize dependency chain
    plan_node = next(n for n in wf["nodes"] if n["id"] == "plan")
    assert plan_node["kind"] == "agent"
    research_node = next(n for n in wf["nodes"] if n["id"] == "research")
    assert "plan" in research_node["depends_on"]
    assert research_node["map_from"] == "plan.tasks"
    assert research_node["max_fan_out"] == 3


def test_extract_manifest_spec_approval_workflow() -> None:
    settings = load_settings(("examples/reference_customer_support/config/agent.yaml",))
    spec = extract_agent_manifest_spec(settings)

    wf = spec["workflows"]["customer-support-resolution"]
    approval_node = next(n for n in wf["nodes"] if n["id"] == "approve-action")
    assert approval_node["kind"] == "approval"
    assert approval_node["approval"] is not None
    assert "Approve the proposed customer response" in approval_node["approval"]["prompt"]


def test_generate_agent_ui_html() -> None:
    import re

    settings = load_settings(("examples/pattern_multi_agent_fanout/agent.yaml",))
    html = generate_agent_ui_html(settings=settings, title="Custom Multi-Agent Explorer")

    assert "<!DOCTYPE html>" in html
    assert "Custom Multi-Agent Explorer" in html
    assert "fanout-planner" in html
    assert "multi-agent-fanout" in html
    assert "agent.yaml" in html
    # Check that template tokens were fully replaced
    assert "__PAGE_TITLE__" not in html
    assert "__SPEC_JSON__" not in html

    # Verify ZERO emojis in generated HTML
    emoji_pattern = re.compile(r"[\U00010000-\U0010ffff]", flags=re.UNICODE)
    assert emoji_pattern.findall(html) == []

    # Verify Studio look and feel & read-only execution tab content
    assert "Algen Agent Studio" in html
    assert "Read-Only" in html or "read-only" in html.lower()
    assert "algen-agent-studio" in html
    assert "<svg" in html


def test_export_agent_ui_html(tmp_path: Path) -> None:
    target = tmp_path / "exported-ui.html"
    result_path = export_agent_ui_html(
        config_files=("examples/quickstart_local_chat/agent.yaml",),
        output_path=target,
        title="Quickstart Chat Spec",
    )

    assert result_path == target
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "Quickstart Chat Spec" in content
    assert "minimal" in content


def test_cli_ui_command(tmp_path: Path) -> None:
    target = tmp_path / "cli-ui.html"
    main(
        [
            "ui",
            "--config",
            "examples/quickstart_local_chat/agent.yaml",
            "--output",
            str(target),
            "--title",
            "CLI Generated UI",
        ]
    )

    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "CLI Generated UI" in content
    assert "local-private" in content


def test_cli_export_ui_alias(tmp_path: Path) -> None:
    target = tmp_path / "alias-ui.html"
    main(
        [
            "export-ui",
            "--config",
            "examples/quickstart_local_chat/agent.yaml",
            "--output",
            str(target),
        ]
    )

    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "minimal" in content


def test_cli_ui_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    output_buffer = io.StringIO()
    monkeypatch.setattr("sys.stdout", output_buffer)

    main(
        [
            "ui",
            "--config",
            "examples/quickstart_local_chat/agent.yaml",
            "--output",
            "-",
        ]
    )

    result = output_buffer.getvalue()
    assert "<!DOCTYPE html>" in result
    assert "minimal" in result


def test_cli_parser_ui_options() -> None:
    parser = build_parser()
    args_ui = parser.parse_args(["ui", "--output", "out.html", "--title", "Test Title"])
    assert args_ui.command == "ui"
    assert args_ui.output == "out.html"
    assert args_ui.title == "Test Title"

    args_serve = parser.parse_args(["serve", "--ui"])
    assert args_serve.command == "serve"
    assert args_serve.agent_ui is True


@pytest.mark.asyncio
async def test_fastapi_agent_ui_disabled_by_default() -> None:
    settings = AppSettings()
    assert settings.api.agent_ui_enabled is False

    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/agent-ui")
        assert response.status_code == 404
        assert "disabled" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_fastapi_agent_ui_enabled() -> None:
    settings = load_settings(("examples/pattern_multi_agent_fanout/agent.yaml",))
    settings = settings.model_copy(
        update={"api": settings.api.model_copy(update={"agent_ui_enabled": True})}
    )

    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Accessing HTML page
        html_resp = await client.get("/agent-ui")
        assert html_resp.status_code == 200
        assert "text/html" in html_resp.headers["content-type"]
        assert "multi-agent-fanout" in html_resp.text

        # Accessing with trailing slash
        slash_resp = await client.get("/agent-ui/")
        assert slash_resp.status_code == 200

        # Accessing alias /ui
        alias_resp = await client.get("/ui")
        assert alias_resp.status_code == 200

        # Accessing JSON spec
        spec_resp = await client.get("/agent-ui/spec")
        assert spec_resp.status_code == 200
        data = spec_resp.json()
        assert data["title"]
        assert "multi-agent-fanout" in data["workflows"]
        assert data["live_server"] is True
