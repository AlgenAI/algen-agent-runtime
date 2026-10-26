from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from algen_agent_runtime.cli import main
from algen_agent_runtime.orchestration.check import check_project, scan_project_code

REPOSITORY = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIGS = sorted(
    path
    for path in (REPOSITORY / "examples").rglob("agent.yaml")
    if "__pycache__" not in path.parts
)


def _project_root(config: Path) -> Path:
    return config.parent.parent if config.parent.name == "config" else config.parent


@pytest.mark.parametrize(
    "config", EXAMPLE_CONFIGS, ids=[str(p.relative_to(REPOSITORY)) for p in EXAMPLE_CONFIGS]
)
def test_every_example_project_passes_check(config: Path) -> None:
    report = check_project((config,), project_root=_project_root(config))

    assert report.ok, [issue.message for issue in report.errors]
    assert report.inventory is not None


def test_examples_are_found() -> None:
    assert len(EXAMPLE_CONFIGS) >= 15


AGENT_YAML = """
providers:
  local: {type: mock, default_model: m}
agents:
  - name: operator
    version: 1.0.0
    description: d
    system_instructions: x
    default_model: {name: m, provider: local}
    enabled_tools: [core.htp]
"""


def _write(tmp_path: Path, text: str, name: str = "agent.yaml") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def test_unknown_names_are_errors_with_a_suggestion(tmp_path: Path) -> None:
    report = check_project((_write(tmp_path, AGENT_YAML),))

    assert not report.ok
    [issue] = report.errors
    assert (issue.code, issue.agent, issue.field, issue.name, issue.suggestion) == (
        "UNKNOWN_COMPONENT",
        "operator",
        "enabled_tools",
        "core.htp",
        "core.http",
    )


def test_unsupported_provider_type_is_an_error(tmp_path: Path) -> None:
    config = _write(
        tmp_path, AGENT_YAML.replace("type: mock", "type: mocc").replace("core.htp", "core.http")
    )

    report = check_project((config,))

    assert [(i.code, i.suggestion) for i in report.errors] == [
        ("UNSUPPORTED_PROVIDER_TYPE", "mock")
    ]


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        ("unknown_key: 1\n", "CONFIGURATION_ERROR"),
        ("security: {trusted_plugin_prefixes: [nowhere]}\n", "PLUGIN_ERROR"),
    ],
)
def test_settings_and_plugin_failures_are_errors(tmp_path: Path, extra: str, code: str) -> None:
    text = AGENT_YAML.replace("core.htp", "core.http") + extra
    if code == "PLUGIN_ERROR":
        text = text.replace(
            "enabled_tools: [core.http]",
            'enabled_tools: [core.http]\n    domain_plugins: ["nowhere.missing:Plugin"]',
        )

    report = check_project((_write(tmp_path, text),))

    assert [issue.code for issue in report.errors] == [code]


def test_studio_extensions_are_ignored(tmp_path: Path) -> None:
    text = AGENT_YAML.replace("core.htp", "core.http") + "extensions: {studio: {a: 1}}\n"

    assert check_project((_write(tmp_path, text),)).ok


CODE = """
import os
from os import getenv

from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.tools.starter import register_starter_tools
from algen_agent_runtime.types.contracts import AgentDefinition

token = os.environ["TOKEN"]
container = build_container(settings, additional_tools=tools, mcp_servers=servers)
container.tools.register(tool)
container.runtime.router.register_provider(provider)
register_starter_tools(container.tools)
agent = AgentDefinition(name="coded")
container.runtime.policies.register(policy)
container.analytical_graphs.register_handler(kind, handler)
container.retrievers.register("kb", store)


class Plugin:
    def register(self, context):
        context.tools.register(tool)
        context.providers.register(provider)
"""


def test_code_scan_warns_about_what_studio_cannot_run(tmp_path: Path) -> None:
    _write(tmp_path, CODE, "src/app.py")
    _write(tmp_path, "container = build_container(settings)\n", "tests/test_app.py")
    _write(tmp_path, "container = build_container(settings)\n", ".venv/lib/x.py")
    _write(tmp_path, "def broken(:\n", "src/broken.py")

    issues = list(scan_project_code(tmp_path))

    found = sorted((issue.code, issue.location) for issue in issues)
    assert found == [
        ("CODE_DEFINED_AGENT", "src/app.py:14"),
        ("CONSTRUCTOR_COMPONENTS", "src/app.py:10"),
        ("DIRECT_ENV_READ", "src/app.py:3"),
        ("OWN_CONTAINER", "src/app.py:10"),
        ("POST_BUILD_REGISTRATION", "src/app.py:11"),
        ("POST_BUILD_REGISTRATION", "src/app.py:12"),
        ("POST_BUILD_REGISTRATION", "src/app.py:13"),
        ("POST_BUILD_REGISTRATION", "src/app.py:15"),
        ("POST_BUILD_REGISTRATION", "src/app.py:16"),
        ("POST_BUILD_REGISTRATION", "src/app.py:17"),
        ("UNREADABLE_SOURCE", "src/broken.py"),
    ]
    env = next(issue for issue in issues if issue.code == "DIRECT_ENV_READ")
    assert "2 time(s) (lines 3, 9)" in env.message
    assert all(issue.severity == "warning" for issue in issues)


def test_cli_check_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, AGENT_YAML.replace("core.htp", "core.http"))
    _write(tmp_path, "container = build_container(settings)\n", "app.py")

    with pytest.raises(SystemExit) as clean:
        main(["check"])
    assert clean.value.code == 0
    assert "0 error(s), 1 warning(s)" in capsys.readouterr().out

    with pytest.raises(SystemExit) as strict:
        main(["check", "agent.yaml", "--strict", "--json"])
    assert strict.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["issues"][0]["code"] == "OWN_CONTAINER"
    assert {"name": "react", "source": "runtime"} in report["inventory"]["planners"]

    _write(tmp_path, AGENT_YAML)
    with pytest.raises(SystemExit) as failing:
        main(["check", "agent.yaml"])
    assert failing.value.code == 1
    assert "did you mean 'core.http'?" in capsys.readouterr().out
