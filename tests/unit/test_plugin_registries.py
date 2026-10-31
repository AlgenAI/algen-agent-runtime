from __future__ import annotations

import sys
import types
from collections.abc import Mapping
from typing import Any

import pytest

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.exceptions.errors import ConfigurationError, ConflictError
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.orchestration.describe import describe_components
from algen_agent_runtime.plugins.sdk import PluginManifest, plugin
from algen_agent_runtime.policies.contracts import PolicyAction, PolicyDecision, PolicyPoint
from algen_agent_runtime.retrieval.memory import InMemoryRetriever


class ToneGuard:
    name = "tone_guard"
    category = "moderation"
    enforcement_mode = "block"
    applies_to = frozenset({PolicyPoint.INPUT})

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        if isinstance(payload, str) and "shout" in payload:
            return PolicyDecision(
                action=PolicyAction.DENY, reason_code="tone.shouting", reason="Shouting."
            )
        return PolicyDecision(action=PolicyAction.ALLOW, reason_code="tone.ok", reason="Fine.")


class Secrets(ToneGuard):
    name = "secrets"


class DeploymentGuard(ToneGuard):
    name = "deployment_guard"


async def warehouse_query(node: Any, inputs: dict[str, Any], context: Any) -> Any:
    raise AssertionError("not called in these tests")


class ClosingStore(InMemoryRetriever):
    closed = False

    async def close(self) -> None:
        ClosingStore.closed = True


@plugin(PluginManifest(name="acme.pack", version="1.0.0", publisher="acme"))
class PackPlugin:
    def register(self, context: Any) -> None:
        context.policies.register(ToneGuard())
        context.analytical_nodes.register("semantic_query", warehouse_query)
        context.retrievers.register("handbook", ClosingStore(), limit=3)


def _bad(register: Any) -> type:
    return plugin(PluginManifest(name="acme.bad", version="1.0.0", publisher="acme"))(
        type("Bad", (), {"register": lambda self, context: register(context)})
    )


@pytest.fixture(autouse=True)
def acme(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("acme.pack")
    module.PackPlugin = PackPlugin  # type: ignore[attr-defined]
    module.MandatoryGuard = _bad(  # type: ignore[attr-defined]
        lambda c: c.policies.register(DeploymentGuard(), mandatory=True)
    )
    module.ReplacesSecrets = _bad(lambda c: c.policies.register(Secrets()))  # type: ignore[attr-defined]
    module.ReplacesRank = _bad(lambda c: c.analytical_nodes.register("rank", warehouse_query))  # type: ignore[attr-defined]
    module.UnknownKind = _bad(lambda c: c.analytical_nodes.register("forecast", warehouse_query))  # type: ignore[attr-defined]
    module.ClashingStore = _bad(lambda c: c.retrievers.register("docs", InMemoryRetriever()))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "acme", types.ModuleType("acme"))
    monkeypatch.setitem(sys.modules, "acme.pack", module)
    return module


def _settings(plugin_name: str = "PackPlugin", **agent: Any) -> AppSettings:
    return AppSettings.model_validate(
        {
            "providers": {"local": {"type": "mock", "default_model": "m"}},
            "agents": [
                {
                    "name": "operator",
                    "version": "1.0.0",
                    "description": "d",
                    "system_instructions": "x",
                    "default_model": {"name": "m", "provider": "local"},
                    "domain_plugins": [f"acme.pack:{plugin_name}"],
                    **agent,
                }
            ],
            "retrieval": {"docs": {"type": "memory"}},
            "security": {"trusted_plugin_prefixes": ["acme"]},
            "runtime": {"recover_incomplete_runs": False},
        }
    )


async def test_a_plugin_policy_guards_the_agents_that_name_it() -> None:
    settings = _settings(guardrail_policy={"policies": ["secrets", "tone_guard"]})
    container = build_container(settings)
    agent = settings.agents[0]

    decision = await container.runtime.policies.evaluate("input", "please shout", {"agent": agent})

    assert decision.action == PolicyAction.DENY
    assert container.component_sources["policies"] == {"tone_guard": "acme.pack:PackPlugin"}


async def test_a_plugin_mandatory_policy_guards_agents_that_do_not_name_it() -> None:
    settings = _settings("MandatoryGuard")
    container = build_container(settings)
    agent = settings.agents[0]

    decision = await container.runtime.policies.evaluate("input", "please shout", {"agent": agent})

    assert decision.action == PolicyAction.DENY
    assert container.component_sources["policies"] == {
        "deployment_guard": "acme.pack:MandatoryGuard"
    }


@pytest.mark.parametrize(
    ("guardrails", "message"),
    [
        (
            {"policies": ["secrets", "tone_gaurd"]},
            "guardrail_policy.policies 'tone_gaurd' is not registered; did you mean 'tone_guard'?",
        ),
        (
            {"tool_policies": ["secret"]},
            "guardrail_policy.tool_policies 'secret' is not registered; did you mean 'secrets'?",
        ),
    ],
)
def test_an_unregistered_guardrail_fails_the_build(
    guardrails: dict[str, Any], message: str
) -> None:
    with pytest.raises(ConfigurationError) as error:
        build_container(_settings(guardrail_policy=guardrails))

    assert message in str(error.value)


def test_a_plugin_supplies_a_missing_analytical_node_handler() -> None:
    container = build_container(_settings())

    assert container.analytical_graphs is not None
    assert "semantic_query" in {kind.value for kind in container.analytical_graphs.handler_kinds()}
    assert container.component_sources["analytical_nodes"] == {
        "semantic_query": "acme.pack:PackPlugin"
    }


async def test_a_plugin_search_store_gets_a_context_builder_and_closes_with_the_container() -> None:
    ClosingStore.closed = False
    container = build_container(_settings(context_builder="retrieval.handbook"))

    assert isinstance(container.retrievers.get("handbook"), ClosingStore)
    assert "retrieval.handbook" in container.runtime.contexts.names()
    assert container.component_sources["context_builders"] == {
        "retrieval.handbook": "acme.pack:PackPlugin"
    }
    await container.aclose()
    assert ClosingStore.closed is True


@pytest.mark.parametrize(
    ("plugin_name", "error", "message"),
    [
        ("ReplacesSecrets", ConflictError, "policies 'secrets' is already registered"),
        ("ReplacesRank", ConflictError, "analytical_nodes 'rank' is already registered"),
        ("UnknownKind", ConfigurationError, "unknown analytical node kind 'forecast'"),
        ("ClashingStore", ConflictError, "retrievers 'docs' is already registered"),
    ],
)
def test_plugins_cannot_replace_or_invent(
    plugin_name: str, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        build_container(_settings(plugin_name))
    with pytest.raises(error, match=message):
        describe_components(_settings(plugin_name))


def test_the_inventory_lists_all_three_like_the_container() -> None:
    settings = _settings(guardrail_policy={"policies": ["tone_guard"]})
    inventory = describe_components(settings)
    container = build_container(settings)

    assert [p.name for p in inventory.policies] == sorted(container.runtime.policies.list())
    assert {p.name: p.source for p in inventory.policies}["tone_guard"] == "acme.pack:PackPlugin"
    assert container.analytical_graphs is not None
    assert {n.name for n in inventory.analytical_nodes} == {
        kind.value for kind in container.analytical_graphs.handler_kinds()
    }
    assert [(r.name, r.source) for r in inventory.retrievers] == [
        ("docs", "manifest"),
        ("handbook", "acme.pack:PackPlugin"),
    ]
    assert list(container.retrievers.list()) == ["docs", "handbook"]
