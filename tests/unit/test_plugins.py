from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from algen_agent_runtime.config.settings import AppSettings, ProviderSettings, SecuritySettings
from algen_agent_runtime.exceptions.errors import ConfigurationError, ConflictError
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.planning.planners import DirectPlanner
from algen_agent_runtime.plugins.sdk import PluginLoader, PluginManifest, is_trusted, plugin
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.types.contracts import AgentDefinition, ModelProfile


def _tool(name: str) -> Tool:
    async def handler(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        return {}

    return Tool(
        ToolDefinition(
            name=name,
            version="1.0.0",
            description="A test tool.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
            side_effect=SideEffect.NONE,
        ),
        handler,
    )


class _CustomPlanner(DirectPlanner):
    name = "custom"


class _Resource:
    closed = False

    async def aclose(self) -> None:
        self.closed = True


def _manifest(name: str) -> PluginManifest:
    return PluginManifest(name=name, version="1.0.0", publisher="tests")


@plugin(_manifest("acme.crm"))
class CrmPlugin:
    resource = _Resource()

    def register(self, context: Any) -> None:
        context.tools.register(_tool("crm.find_account"))
        context.planners.register(_CustomPlanner())
        context.providers.register(MockModelProvider(), registration_id="acme-mock")
        context.add_resource(self.resource)


@plugin(_manifest("acme.shadow"))
class ShadowPlugin:
    def register(self, context: Any) -> None:
        context.planners.register(DirectPlanner())


@plugin(_manifest("acme.http"))
class HttpShadowPlugin:
    def register(self, context: Any) -> None:
        context.tools.register(_tool("core.http"))


@plugin(_manifest("acme.mcp"))
class McpPlugin:
    def register(self, context: Any) -> None:
        from algen_agent_runtime.mcp.contracts import MCPStdioServerConfig

        context.mcp_servers.register(MCPStdioServerConfig(name="files", command="files-mcp"))


@pytest.fixture
def acme(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("acme.plugins")
    for cls in (CrmPlugin, ShadowPlugin, HttpShadowPlugin, McpPlugin):
        setattr(module, cls.__name__, cls)
    module.not_a_plugin = 42  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "acme", types.ModuleType("acme"))
    monkeypatch.setitem(sys.modules, "acme.plugins", module)
    return module


def _settings(*plugins: str, trusted: tuple[str, ...] = ("acme",)) -> AppSettings:
    return AppSettings(
        agents=(
            AgentDefinition(
                name="operator",
                version="1.0.0",
                description="d",
                system_instructions="x",
                default_model=ModelProfile(name="m", provider="mock"),
                domain_plugins=plugins,
            ),
        ),
        providers={"mock": ProviderSettings(type="mock", default_model="m")},
        security=SecuritySettings(trusted_plugin_prefixes=trusted),
    )


def test_declared_plugin_registers_components(acme: types.ModuleType) -> None:
    container = build_container(_settings("acme.plugins:CrmPlugin"))

    assert container.tools.get("crm.find_account").definition.name == "crm.find_account"
    assert "custom" in container.runtime.planners.names()
    assert container.runtime.router.provider("acme-mock").provider_id == "mock"
    assert CrmPlugin.resource in container.resources
    assert container.plugins == (_manifest("acme.crm"),)
    assert container.component_sources == {
        "tools": {"crm.find_account": "acme.plugins:CrmPlugin"},
        "planners": {"custom": "acme.plugins:CrmPlugin"},
        "providers": {"acme-mock": "acme.plugins:CrmPlugin"},
    }


def test_plugin_named_by_several_agents_loads_once(acme: types.ModuleType) -> None:
    settings = _settings("acme.plugins:CrmPlugin")
    second = settings.agents[0].model_copy(update={"name": "second"})
    settings = settings.model_copy(update={"agents": (*settings.agents, second)})

    container = build_container(settings)

    assert len(container.plugins) == 1


def test_constructor_arguments_still_work_alongside_plugins(acme: types.ModuleType) -> None:
    container = build_container(
        _settings("acme.plugins:CrmPlugin"), additional_tools=(_tool("legacy.tool"),)
    )

    assert {"crm.find_account", "legacy.tool"} <= {
        tool.definition.name for tool in container.tools.list()
    }


def test_constructor_tool_with_a_plugin_tool_name_conflicts(acme: types.ModuleType) -> None:
    with pytest.raises(ConflictError, match=r"crm\.find_account"):
        build_container(
            _settings("acme.plugins:CrmPlugin"), additional_tools=(_tool("crm.find_account"),)
        )


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        ("acme.plugins:ShadowPlugin", "planners 'direct' is already registered"),
        ("acme.plugins:HttpShadowPlugin", "tools 'core.http' is already registered"),
    ],
)
def test_plugin_cannot_replace_a_registered_component(
    acme: types.ModuleType, reference: str, message: str
) -> None:
    with pytest.raises(ConflictError, match=message):
        build_container(_settings(reference))


def test_two_plugins_registering_one_name_conflict(acme: types.ModuleType) -> None:
    acme.CrmAgain = plugin(_manifest("acme.crm2"))(type("CrmAgain", (CrmPlugin,), {}))  # type: ignore[attr-defined]

    with pytest.raises(ConflictError, match=r"crm\.find_account"):
        build_container(_settings("acme.plugins:CrmPlugin", "acme.plugins:CrmAgain"))


def test_plugins_require_trusted_prefixes(acme: types.ModuleType) -> None:
    with pytest.raises(ConfigurationError, match="trusted_plugin_prefixes"):
        build_container(_settings("acme.plugins:CrmPlugin", trusted=()))


def test_untrusted_plugin_is_refused_before_import(monkeypatch: pytest.MonkeyPatch) -> None:
    imported: list[str] = []
    monkeypatch.setattr(
        "algen_agent_runtime.plugins.sdk.importlib.import_module",
        lambda name: imported.append(name),
    )

    with pytest.raises(ConfigurationError, match="not from a trusted namespace"):
        build_container(_settings("acme.plugins:CrmPlugin", "evil.plugins:Steal"))
    assert imported == []


@pytest.mark.parametrize(
    ("reference", "prefixes", "trusted"),
    [
        ("acme.plugins:CrmPlugin", ("acme",), True),
        ("acme.plugins:CrmPlugin", ("acme.",), True),
        ("acme", ("acme",), True),
        ("acme_evil.plugins:CrmPlugin", ("acme",), False),
        ("acme.pluginsx:CrmPlugin", ("acme.plugins",), False),
        ("other.acme:CrmPlugin", ("acme",), False),
    ],
)
def test_trust_matches_on_module_boundaries(
    reference: str, prefixes: tuple[str, ...], trusted: bool
) -> None:
    assert is_trusted(reference, prefixes) is trusted


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        ("acme.plugins:Missing", "has no attribute 'Missing'"),
        ("acme.plugins:not_a_plugin", "neither a plugin nor a factory"),
        ("acme.missing:Plugin", "could not import plugin module"),
        ("acme.not_installed", "was not found in group"),
    ],
)
def test_bad_plugin_references_are_configuration_errors(
    acme: types.ModuleType, reference: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        build_container(_settings(reference))


def test_plugin_without_manifest_is_refused(acme: types.ModuleType) -> None:
    class Bare:
        def register(self, context: Any) -> None:
            pass

    acme.Bare = Bare  # type: ignore[attr-defined]

    with pytest.raises(ConfigurationError, match="must have a PluginManifest"):
        build_container(_settings("acme.plugins:Bare"))


def test_incompatible_core_api_is_refused(acme: types.ModuleType) -> None:
    acme.Future = plugin(  # type: ignore[attr-defined]
        PluginManifest(name="f", version="1", publisher="t", core_api_version="2")
    )(type("Future", (), {"register": lambda self, context: None}))

    with pytest.raises(ConfigurationError, match="incompatible core API"):
        build_container(_settings("acme.plugins:Future"))


def test_loader_loads_instances_and_factories(acme: types.ModuleType) -> None:
    acme.instance = CrmPlugin()  # type: ignore[attr-defined]
    acme.factory = lambda: CrmPlugin()  # type: ignore[attr-defined]
    registered: list[str] = []
    context = types.SimpleNamespace(
        tools=types.SimpleNamespace(register=lambda tool: registered.append(tool.definition.name)),
        planners=types.SimpleNamespace(register=lambda planner: None),
        providers=types.SimpleNamespace(register=lambda *args, **kwargs: None),
        add_resource=lambda resource: None,
    )

    PluginLoader(("acme",)).load(("acme.plugins:instance", "acme.plugins:factory"), context)

    assert registered == ["crm.find_account", "crm.find_account"]


def test_plugin_mcp_servers_reach_the_client_manager(acme: types.ModuleType) -> None:
    pytest.importorskip("mcp")

    container = build_container(_settings("acme.plugins:McpPlugin"))

    assert container.mcp is not None
    assert container.mcp.server_names == ("files",)
    assert container.component_sources == {"mcp_servers": {"files": "acme.plugins:McpPlugin"}}
