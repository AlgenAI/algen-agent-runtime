from __future__ import annotations

import json
import sys
import textwrap
import types
from pathlib import Path
from typing import Any

import pytest

from algen_agent_runtime.cli import main
from algen_agent_runtime.config.settings import AppSettings, SecuritySettings
from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.orchestration.describe import describe_components
from algen_agent_runtime.planning.planners import DirectPlanner
from algen_agent_runtime.plugins.sdk import PluginManifest, plugin
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition

CALLS: list[str] = []


class _CustomPlanner(DirectPlanner):
    name = "custom"

    async def plan(self, *args: Any, **kwargs: Any) -> Any:
        CALLS.append("planner")
        raise AssertionError("describe must not call a planner")


class _Resource:
    def start(self) -> None:
        CALLS.append("resource")


def _write_tool() -> Tool:
    async def handler(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        CALLS.append("tool")
        raise AssertionError("describe must not call a tool")

    return Tool(
        ToolDefinition(
            name="crm.update_owner",
            version="1.2.0",
            description="Change an account's owner.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
            required_permissions=frozenset({"crm.write"}),
            side_effect=SideEffect.WRITE,
            timeout_seconds=20,
        ),
        handler,
    )


@plugin(
    PluginManifest(name="acme.crm", version="2.0.0", publisher="acme", env=frozenset({"CRM_TOKEN"}))
)
class CrmPlugin:
    def register(self, context: Any) -> None:
        context.tools.register(_write_tool())
        context.planners.register(_CustomPlanner())
        context.providers.register(MockModelProvider(), registration_id="crm-model")
        context.add_resource(_Resource())


@plugin(PluginManifest(name="acme.clash", version="1.0.0", publisher="acme"))
class ProviderClashPlugin:
    def register(self, context: Any) -> None:
        context.providers.register(MockModelProvider(), registration_id="primary")


@pytest.fixture(autouse=True)
def acme(monkeypatch: pytest.MonkeyPatch) -> None:
    CALLS.clear()
    module = types.ModuleType("acme.plugins")
    module.CrmPlugin = CrmPlugin  # type: ignore[attr-defined]
    module.ProviderClashPlugin = ProviderClashPlugin  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "acme", types.ModuleType("acme"))
    monkeypatch.setitem(sys.modules, "acme.plugins", module)


def _settings(*plugins: str) -> AppSettings:
    return AppSettings.model_validate(
        {
            "providers": {
                "primary": {"type": "anthropic", "default_model": "m"},
                "local": {"type": "mock", "default_model": "m"},
            },
            "agents": [
                {
                    "name": "operator",
                    "version": "1.0.0",
                    "description": "d",
                    "system_instructions": "x",
                    "default_model": {"name": "m", "provider": "local"},
                    "domain_plugins": list(plugins),
                }
            ],
            "http_tools": [
                {
                    "name": "github.issue",
                    "description": "Read the rate limit.",
                    "url": "https://api.github.com/rate_limit",
                    "auth": {"type": "bearer", "token": "env://GITHUB_TOKEN"},
                }
            ],
            "storage": {"run_store": "postgres", "postgres_dsn": "env://DATABASE_URL"},
            "security": SecuritySettings(
                allowed_http_hosts=("api.github.com",), trusted_plugin_prefixes=("acme",)
            ),
        }
    )


def test_inventory_lists_every_source_by_kind() -> None:
    inventory = describe_components(_settings("acme.plugins:CrmPlugin"))

    tools = {tool.name: tool for tool in inventory.tools}
    sources = {name: tool.source for name, tool in tools.items()}
    assert sources["crm.update_owner"] == "acme.plugins:CrmPlugin"
    assert sources["github.issue"] == "manifest"
    assert {name for name, source in sources.items() if source == "runtime"} >= {
        "core.http",
        "core.subprocess",
    }
    assert all(name.startswith("core.") for name, s in sources.items() if s == "runtime")
    write = tools["crm.update_owner"]
    assert write.side_effect == SideEffect.WRITE
    assert write.required_permissions == ("crm.write",)
    assert write.timeout_seconds == 20
    assert write.input_schema["additionalProperties"] is False
    assert write.env == ("CRM_TOKEN",)
    assert tools["github.issue"].env == ("GITHUB_TOKEN",)

    planners = {planner.name: planner.source for planner in inventory.planners}
    assert planners == {
        "custom": "acme.plugins:CrmPlugin",
        "direct": "runtime",
        "react": "runtime",
        "rule_based": "runtime",
    }
    assert [(p.name, p.type, p.source) for p in inventory.providers] == [
        ("crm-model", None, "acme.plugins:CrmPlugin"),
        ("local", "mock", "manifest"),
        ("primary", "anthropic", "manifest"),
    ]
    assert "anthropic" in inventory.provider_types
    assert [(p.reference, p.name, p.env) for p in inventory.plugins] == [
        ("acme.plugins:CrmPlugin", "acme.crm", ("CRM_TOKEN",))
    ]
    assert inventory.environment == (
        "ANTHROPIC_API_KEY",
        "CRM_TOKEN",
        "DATABASE_URL",
        "GITHUB_TOKEN",
    )


def test_describe_calls_no_component_and_resolves_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("ANTHROPIC_API_KEY", "CRM_TOKEN", "DATABASE_URL", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    describe_components(_settings("acme.plugins:CrmPlugin"))

    assert CALLS == []


def test_inventory_matches_what_the_container_registers() -> None:
    settings = _settings("acme.plugins:CrmPlugin").model_copy(
        update={"storage": AppSettings().storage}
    )
    inventory = describe_components(settings)
    container = build_container(settings, environment={"ANTHROPIC_API_KEY": "k"})
    runtime = container.runtime

    assert [tool.name for tool in inventory.tools] == sorted(
        tool.definition.name for tool in container.tools.list()
    )
    assert [p.name for p in inventory.planners] == list(runtime.planners.names())
    assert [c.name for c in inventory.response_composers] == list(runtime.composers.names())
    assert [v.name for v in inventory.verifiers] == list(runtime.verifiers.names())
    assert [c.name for c in inventory.context_builders] == list(runtime.contexts.names())


def test_plugin_provider_cannot_take_a_manifest_provider_name() -> None:
    with pytest.raises(ConflictError, match="provider 'primary'"):
        describe_components(_settings("acme.plugins:ProviderClashPlugin"))


PLUGIN_SOURCE = textwrap.dedent(
    """
    from algen_agent_runtime.plugins.sdk import PluginManifest, plugin


    @plugin(PluginManifest(name="describecli.tools", version="1.0.0", publisher="tests"))
    class Tools:
        def register(self, context):
            from algen_agent_runtime.planning.planners import DirectPlanner

            class Quick(DirectPlanner):
                name = "quick"

            context.planners.register(Quick())
    """
)

AGENT_YAML = textwrap.dedent(
    """
    providers:
      local: {type: mock, default_model: m}
    security:
      trusted_plugin_prefixes: [describecli]
    agents:
      - name: operator
        version: 1.0.0
        description: d
        system_instructions: x
        default_model: {name: m, provider: local}
        domain_plugins: ["describecli.plugin:Tools"]
    """
)


def test_cli_describe_imports_plugins_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    package = tmp_path / "src" / "describecli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "plugin.py").write_text(PLUGIN_SOURCE)
    (tmp_path / "agent.yaml").write_text(AGENT_YAML)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        main(["describe", "--path", "src", "--json"])

    assert exit_info.value.code == 0
    inventory = json.loads(capsys.readouterr().out)
    assert {"name": "quick", "source": "describecli.plugin:Tools"} in inventory["planners"]
    for name in ("describecli", "describecli.plugin"):
        sys.modules.pop(name, None)


def test_cli_describe_reports_configuration_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "agent.yaml").write_text(AGENT_YAML.replace("[describecli]", "[other]"))
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        main(["describe"])

    assert exit_info.value.code == 1
    assert "not from a trusted namespace" in capsys.readouterr().err
