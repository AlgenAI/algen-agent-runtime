"""HTTP reconciliation routes: scope, tenant isolation, conflicts and content-minimal views."""

from __future__ import annotations

from typing import Any

import httpx
from conftest import make_agent, make_runtime

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.conversations.service import (
    ConversationHandlerRegistry,
    ConversationService,
)
from algen_agent_runtime.conversations.stores import (
    InMemoryConversationEventBus,
    InMemoryConversationStore,
)
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import Container
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolResult,
)
from algen_agent_runtime.types.contracts import ApprovalPolicy, RunRequest, RunStatus, ToolPolicy

TOOL = "crm.update"
OPERATOR = {"x-tenant-id": "tenant", "x-user-id": "ops-1", "x-scopes": "tools:reconcile"}


def broken(arguments: dict[str, Any], context: ToolContext) -> Any:
    raise ConnectionError("reset after submit")


async def paused_container(compensated: list[ToolResult]) -> tuple[Container, str, str]:
    runtime = make_runtime(
        MockModelProvider([tool_call_response(TOOL, {"id": "secret-account"}), "done"]),
        make_agent(
            enabled_tools=frozenset({TOOL}),
            approval_policy=ApprovalPolicy(require_for_side_effects=False),
            tool_policy=ToolPolicy(on_indeterminate="pause"),
        ),
    )
    runtime.tools.register(
        Tool(
            ToolDefinition(
                name=TOOL,
                version="1",
                description="update",
                input_schema={"type": "object", "properties": {"id": {"type": "string"}}},
                output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
                side_effect=SideEffect.EXTERNAL,
                idempotency=Idempotency.KEYED,
                retry_policy={"max_attempts": 1},
            ),
            broken,
            lambda result, context: compensated.append(result),
        )
    )
    result = await runtime.run(
        RunRequest(agent="test-agent", input="x", tenant_id="tenant", user_id="user")
    )
    assert result.status == RunStatus.AWAITING_RECONCILIATION
    state = await runtime.status(result.run_id, "tenant")
    container = Container(
        runtime=runtime,
        agents=runtime.agents,
        tools=runtime.tools,
        artifacts=InMemoryArtifactStore(),
        events=runtime.events,
        conversations=ConversationService(
            InMemoryConversationStore(),
            InMemoryConversationEventBus(),
            ConversationHandlerRegistry(),
        ),
    )
    return container, result.run_id, str((state.pause_payload or {})["idempotency_key"])


async def test_reconciliation_routes_need_scope_and_stay_in_the_tenant() -> None:
    container, run_id, key = await paused_container([])
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        no_scope = {**OPERATOR, "x-scopes": "runs:read runs:write"}
        assert (await client.get("/v1/tool-executions", headers=no_scope)).status_code == 403
        assert (
            await client.post(
                f"/v1/tool-executions/{key}/resolve",
                headers=no_scope,
                json={"status": "failed", "reason": "x"},
            )
        ).status_code == 403

        other = {**OPERATOR, "x-tenant-id": "other"}
        listed = await client.get("/v1/tool-executions", headers=other)
        assert listed.json() == {"executions": []}
        assert (await client.get(f"/v1/tool-executions/{key}", headers=other)).status_code == 404
        assert (
            await client.post(
                f"/v1/tool-executions/{key}/resolve",
                headers=other,
                json={"status": "failed", "reason": "x"},
            )
        ).status_code == 404

        listed = await client.get(
            "/v1/tool-executions",
            headers=OPERATOR,
            params={"status": ["indeterminate"], "run_id": run_id, "tool": TOOL},
        )
        [view] = listed.json()["executions"]
        assert view["idempotency_key"] == key and view["status"] == "indeterminate"
        assert "secret-account" not in listed.text and "result" not in view


async def test_resolve_resumes_the_run_and_conflicts_are_409() -> None:
    container, run_id, key = await paused_container([])
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        invalid = await client.post(
            f"/v1/tool-executions/{key}/resolve",
            headers=OPERATOR,
            json={"status": "failed", "reason": "x", "result": {"ok": True}},
        )
        assert invalid.status_code == 400
        resolved = await client.post(
            f"/v1/tool-executions/{key}/resolve",
            headers=OPERATOR,
            json={
                "status": "completed",
                "reason": "CRM shows the change",
                "result": {"ok": True},
                "result_reference": "crm-42",
                "expected_status": "indeterminate",
            },
        )
        assert resolved.status_code == 200
        body = resolved.json()
        assert body["outcome"] == "completed" and body["run_resumed"] is True
        execution = body["execution"]
        assert execution["has_result"] is True and execution["result_reference"] == "crm-42"
        assert "ok" not in str(execution.get("result", ""))
        assert execution["resolutions"][0]["actor_id"] == "ops-1"

        again = await client.post(
            f"/v1/tool-executions/{key}/resolve",
            headers=OPERATOR,
            json={"status": "failed", "reason": "late"},
        )
        assert again.status_code == 409
        resume = await client.post(f"/v1/tool-executions/{key}/resume", headers=OPERATOR)
        assert resume.status_code == 409
        no_hook = await client.post(
            f"/v1/tool-executions/{key}/reconcile", headers=OPERATOR, json={}
        )
        assert no_hook.status_code == 409
    result = await container.runtime.wait(run_id, "tenant")
    assert result.status == RunStatus.COMPLETED


async def test_compensate_route_runs_the_handler_once() -> None:
    compensated: list[ToolResult] = []
    container, _, key = await paused_container(compensated)
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing_reason = await client.post(
            f"/v1/tool-executions/{key}/compensate", headers=OPERATOR, json={}
        )
        assert missing_reason.status_code == 422
        first = await client.post(
            f"/v1/tool-executions/{key}/compensate",
            headers=OPERATOR,
            json={"reason": "void it", "resume": False},
        )
        assert first.status_code == 200 and first.json()["outcome"] == "compensated"
        second = await client.post(
            f"/v1/tool-executions/{key}/compensate",
            headers=OPERATOR,
            json={"reason": "void it again"},
        )
        assert second.status_code == 409
        resumed = await client.post(f"/v1/tool-executions/{key}/resume", headers=OPERATOR)
        assert resumed.status_code == 200 and resumed.json()["run_resumed"] is True
    assert len(compensated) == 1
