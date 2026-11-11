"""G62: ``runtime.subagent.enabled`` registers core.subagent; agents still opt in one by one."""

from __future__ import annotations

from typing import Any

import pytest

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import Container, build_container
from algen_agent_runtime.orchestration.describe import describe_components
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.tools.subagent import SUBAGENT_TOOL
from algen_agent_runtime.types.contracts import RunRequest, RunStatus

CALL = tool_call_response(SUBAGENT_TOOL, {"agent": "researcher", "input": "look it up"})


def _agent(name: str, **fields: Any) -> dict[str, Any]:
    return {
        "name": name,
        "version": "1.0.0",
        "description": "d",
        "system_instructions": "x",
        "default_model": {"name": "m", "provider": "mock", "model": "m"},
        **fields,
    }


def _settings(*, enabled: bool = True, **planner: Any) -> AppSettings:
    delegating = {
        "enabled_tools": [SUBAGENT_TOOL],
        "tool_permissions": ["agents.delegate"],
        "delegates": ["researcher"],
    }
    return AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "m"}},
            "agents": [
                _agent("planner", **{**delegating, **planner}),
                _agent("researcher"),
                _agent("bystander"),
            ],
            "runtime": {
                "recover_incomplete_runs": False,
                "subagent": {"enabled": enabled, "max_depth": 2},
            },
        }
    )


def _model(container: Container, *responses: Any) -> MockModelProvider:
    model = container.runtime.router.provider("mock")
    assert isinstance(model, MockModelProvider)
    model.queue(*responses)
    return model


def _request(agent: str = "planner") -> RunRequest:
    return RunRequest(agent=agent, input="go", tenant_id="t", user_id="alice")


def test_subagent_is_off_by_default() -> None:
    settings = AppSettings.model_validate({"runtime": {}})

    assert settings.runtime.subagent.enabled is False
    assert settings.runtime.subagent.max_depth == 3


async def test_enabled_from_settings_an_agent_delegates_end_to_end() -> None:
    container = build_container(_settings())
    model = _model(container, CALL, "found it", "planner done")

    result = await AlgenAgentRuntimeClient(container.runtime).run(_request())

    assert result.status == RunStatus.COMPLETED, result.error
    assert result.output == "planner done"
    runs = list(container.runtime.runs._runs.values())  # type: ignore[attr-defined]
    [child] = [run for run in runs if run.request.parent_run_id == result.run_id]
    assert child.request.agent == "researcher"
    assert child.status == RunStatus.COMPLETED
    # The planner's model was offered core.subagent; the researcher's was not.
    offered = [{spec.name for spec in request.tools} for request in model.requests]
    assert SUBAGENT_TOOL in offered[0]
    assert SUBAGENT_TOOL not in offered[1]
    assert container.tools.get(SUBAGENT_TOOL).definition.name == SUBAGENT_TOOL
    assert SUBAGENT_TOOL in {tool.name for tool in describe_components(_settings()).tools}


async def test_an_agent_that_did_not_opt_in_is_never_offered_the_tool() -> None:
    container = build_container(_settings())
    model = _model(container, "bystander done")

    result = await container.runtime.run(_request("bystander"))

    assert result.status == RunStatus.COMPLETED, result.error
    assert all(SUBAGENT_TOOL not in {spec.name for spec in r.tools} for r in model.requests)


def test_not_enabled_the_tool_is_absent() -> None:
    settings = _settings(enabled=False)

    container = build_container(
        settings.model_copy(update={"agents": settings.agents[1:]}),
    )

    assert SUBAGENT_TOOL not in {tool.definition.name for tool in container.tools.list()}
    assert SUBAGENT_TOOL not in {tool.name for tool in describe_components(settings).tools}


def test_not_enabled_an_agent_that_enables_it_fails_the_component_check() -> None:
    with pytest.raises(ConfigurationError, match=SUBAGENT_TOOL):
        build_container(_settings(enabled=False))


async def test_a_delegate_outside_the_allowlist_is_refused() -> None:
    container = build_container(_settings(delegates=["bystander"]))
    _model(container, CALL)

    result = await container.runtime.run(_request())

    assert result.status == RunStatus.FAILED
    assert "may not delegate to 'researcher'" in str(result.error)
    runs = list(container.runtime.runs._runs.values())  # type: ignore[attr-defined]
    assert [run.request.agent for run in runs] == ["planner"]


async def test_max_depth_from_settings_bounds_the_chain() -> None:
    # The planner delegates to itself: depth 1, then 2, and the third call is refused.
    container = build_container(_settings(delegates=["planner"]))
    self_call = tool_call_response(SUBAGENT_TOOL, {"agent": "planner", "input": "again"})
    _model(container, self_call, self_call, self_call, "inner done", "outer done")

    await container.runtime.run(_request())

    runs = list(container.runtime.runs._runs.values())  # type: ignore[attr-defined]
    assert len(runs) == 3
    [refused] = [run for run in runs if run.status == RunStatus.FAILED]
    assert refused.request.metadata["depth"] == "2"
    assert "nesting limit 2 reached" in str(refused.error)
