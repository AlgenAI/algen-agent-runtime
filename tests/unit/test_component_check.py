from __future__ import annotations

import logging
import sys
import types
from typing import Any

import pytest
from pydantic import ValidationError

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.orchestration.describe import describe_components
from algen_agent_runtime.orchestration.validation import (
    AvailableComponents,
    unknown_agent_components,
)
from algen_agent_runtime.plugins.sdk import PluginManifest, plugin
from algen_agent_runtime.tools.contracts import Tool, ToolContext, ToolDefinition


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
        ),
        handler,
    )


@plugin(PluginManifest(name="acme.models", version="1.0.0", publisher="acme"))
class ModelsPlugin:
    def register(self, context: Any) -> None:
        context.providers.register(MockModelProvider(), registration_id="acme-llm")
        context.tools.register(_tool("crm.find_account"))


@pytest.fixture(autouse=True)
def acme(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("acme.plugins")
    module.ModelsPlugin = ModelsPlugin  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "acme", types.ModuleType("acme"))
    monkeypatch.setitem(sys.modules, "acme.plugins", module)


def _settings(
    mode: str = "error", mcp_servers: tuple[dict[str, Any], ...] = (), **agent: Any
) -> AppSettings:
    return AppSettings.model_validate(
        {
            "mcp_servers": list(mcp_servers),
            "providers": {"local": {"type": "mock", "default_model": "m"}},
            "agents": [
                {
                    "name": "operator",
                    "version": "1.0.0",
                    "description": "d",
                    "system_instructions": "x",
                    "default_model": {"name": "m", "provider": "local"},
                    **agent,
                }
            ],
            "security": {"trusted_plugin_prefixes": ["acme"]},
            "runtime": {"component_check": mode},
        }
    )


@pytest.mark.parametrize(
    ("agent", "message"),
    [
        (
            {"enabled_tools": ["core.htp"]},
            "enabled_tools 'core.htp' is not registered; did you mean 'core.http'?",
        ),
        (
            {"planning_strategy": "reakt"},
            "planning_strategy 'reakt' is not registered; did you mean 'react'?",
        ),
        (
            {"response_composer": "fancy"},
            "response_composer 'fancy' is not registered",
        ),
        (
            {"verification_policy": {"verifiers": ["non_empty", "json_schem"]}},
            "verification_policy.verifiers 'json_schem' is not registered; "
            "did you mean 'json_schema'?",
        ),
        (
            {"context_builder": "retrieval.docs"},
            "context_builder 'retrieval.docs' is not registered",
        ),
        (
            {
                "domain_plugins": ["acme.plugins:ModelsPlugin"],
                "default_model": {"name": "m", "provider": "acme-lm"},
            },
            "default_model.provider 'acme-lm' is not registered; did you mean 'acme-llm'?",
        ),
        (
            {
                "domain_plugins": ["acme.plugins:ModelsPlugin"],
                "fallback_models": [{"name": "m", "provider": "other"}],
            },
            "fallback_models[0].provider 'other' is not registered",
        ),
    ],
)
def test_unknown_names_fail_the_build_with_the_closest_match(
    agent: dict[str, Any], message: str
) -> None:
    with pytest.raises(ConfigurationError, match="unregistered agent components") as error:
        build_container(_settings(**agent))

    assert f"agent 'operator': {message}" in str(error.value)


@pytest.mark.parametrize(
    "agent",
    [
        {"enabled_tools": ["core.http"], "tool_permissions": ["network.http"]},
        {"planning_strategy": "rule_based"},
        {"response_composer": "default"},
        {"verification_policy": {"verifiers": ["non_empty", "json_schema", "citations"]}},
        {"context_builder": "default"},
        {
            "domain_plugins": ["acme.plugins:ModelsPlugin"],
            "default_model": {"name": "m", "provider": "acme-llm"},
            "fallback_models": [{"name": "m", "provider": "local"}],
            "enabled_tools": ["crm.find_account"],
        },
    ],
)
def test_registered_names_pass(agent: dict[str, Any]) -> None:
    build_container(_settings(**agent))


def test_every_problem_is_reported_at_once() -> None:
    with pytest.raises(ConfigurationError) as error:
        build_container(_settings(enabled_tools=["a.one", "b.two"], planning_strategy="nope"))

    for name in ("'a.one'", "'b.two'", "'nope'"):
        assert name in str(error.value)


def test_constructor_tools_count_as_registered() -> None:
    build_container(
        _settings(enabled_tools=["legacy.tool"]), additional_tools=(_tool("legacy.tool"),)
    )


def test_warn_mode_logs_and_builds(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        build_container(_settings("warn", enabled_tools=["core.htp"]))

    assert "enabled_tools 'core.htp' is not registered" in caplog.text


def test_unknown_provider_without_plugins_is_still_a_settings_error() -> None:
    with pytest.raises(ValidationError, match="invalid provider references"):
        _settings(default_model={"name": "m", "provider": "acme-llm"})


def test_the_inventory_finds_the_same_problems_as_the_build() -> None:
    settings = _settings(
        mcp_servers=({"name": "files", "transport": "stdio", "command": "files-mcp"},),
        domain_plugins=["acme.plugins:ModelsPlugin"],
        enabled_tools=["crm.find_acount", "mcp.files.read"],
        default_model={"name": "m", "provider": "acme-llm"},
    )

    problems = unknown_agent_components(
        settings.agents, AvailableComponents.from_inventory(describe_components(settings))
    )

    assert [(p.field, p.name, p.suggestion) for p in problems] == [
        ("enabled_tools", "crm.find_acount", "crm.find_account")
    ]


async def test_mcp_tools_are_checked_when_the_server_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("mcp")
    container = build_container(
        _settings(
            mcp_servers=({"name": "files", "transport": "stdio", "command": "files-mcp"},),
            enabled_tools=["mcp.files.read", "mcp.files.wrte"],
        )
    )
    assert container.mcp is not None

    async def discover(server: str) -> list[Tool]:
        return [_tool("mcp.files.read"), _tool("mcp.files.write")]

    monkeypatch.setattr(container.mcp, "discover_tools", discover)

    with pytest.raises(ConfigurationError, match=r"did you mean 'mcp\.files\.write'"):
        await container.astart()
