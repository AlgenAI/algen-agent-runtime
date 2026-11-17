"""Every HTTP route, checked for tenant isolation and authentication (0.2.0b5).

The 0.2.0b3 security assessment found routes that let one tenant act on another's agents and
files (API-1, DATA-1). Those fixes have their own tests; this one keeps the rule from regressing
on routes added later. Every route the app serves must be listed in ``ROUTES`` with how it is
scoped, so a new route fails here until someone decides that. Then:

- a route that is not public refuses a caller without an identity (401);
- tenant B, holding every scope, calls every route with tenant A's ids: a route that names one of
  tenant A's resources answers 403 or 404, and no response carries tenant A's data;
- afterwards tenant A's run, approval, artifact, conversation and workflow run are unchanged.

See AGENTS.md, "Security invariants".
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.attachments import rendered_text
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolDefinition
from algen_agent_runtime.types.contracts import (
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
)

# How each route is scoped:
#   public    served without an identity (health probes, API docs, the agent UI shell, the OAuth
#             redirect, whose state parameter is the credential)
#   platform  needs an identity; serves platform configuration, not tenant data
#   tenant    needs an identity; acts on the caller's tenant only (lists, uploads, admin actions)
#   resource  needs an identity; names a resource in the path, which another tenant cannot reach
ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/openapi.json"): "public",
    ("GET", "/docs"): "public",
    ("GET", "/docs/oauth2-redirect"): "public",
    ("GET", "/redoc"): "public",
    ("GET", "/health/live"): "public",
    ("GET", "/health/ready"): "public",
    ("GET", "/agent-ui/"): "public",
    ("GET", "/agent-ui"): "public",
    ("GET", "/v1/mcp/oauth/callback"): "public",
    ("GET", "/v1/agents"): "platform",
    ("POST", "/v1/agents"): "platform",
    ("GET", "/v1/workflows"): "platform",
    ("GET", "/v1/semantic-layers"): "platform",
    ("GET", "/v1/semantic-layers/{name}"): "platform",
    ("GET", "/v1/mcp/oauth/servers"): "platform",
    ("POST", "/v1/mcp/servers/{server}/oauth/start"): "platform",
    ("POST", "/v1/mcp/oauth/complete"): "platform",
    ("GET", "/v1/mcp/servers/{server}/oauth/status"): "platform",
    ("DELETE", "/v1/mcp/servers/{server}/oauth"): "platform",
    ("POST", "/v1/runs"): "tenant",
    ("GET", "/v1/approvals"): "tenant",
    ("GET", "/v1/storage/usage"): "tenant",
    ("POST", "/v1/artifacts"): "tenant",
    ("GET", "/v1/artifacts"): "tenant",
    ("POST", "/v1/conversations"): "tenant",
    ("GET", "/v1/conversations"): "tenant",
    ("GET", "/v1/conversations/suggestions"): "tenant",
    ("POST", "/v1/workflows/{name}/runs"): "tenant",
    ("GET", "/v1/tool-executions"): "tenant",
    ("POST", "/v1/auth/revocations/tokens"): "tenant",
    ("POST", "/v1/auth/revocations/subjects"): "tenant",
    ("GET", "/v1/admin/users/{user_id}/data"): "tenant",
    ("DELETE", "/v1/admin/users/{user_id}/data"): "tenant",
    ("GET", "/v1/runs/{run_id}"): "resource",
    ("GET", "/v1/runs/{run_id}/narrative"): "resource",
    ("GET", "/v1/runs/{run_id}/events"): "resource",
    ("DELETE", "/v1/runs/{run_id}"): "resource",
    ("POST", "/v1/runs/{run_id}/clarification"): "resource",
    ("POST", "/v1/runs/{run_id}/steer"): "resource",
    ("POST", "/v1/runs/{run_id}/approval"): "resource",
    ("GET", "/v1/approvals/{approval_id}"): "resource",
    ("GET", "/v1/artifacts/{artifact_id}"): "resource",
    ("GET", "/v1/artifacts/{artifact_id}/metadata"): "resource",
    ("PATCH", "/v1/artifacts/{artifact_id}/status"): "resource",
    ("DELETE", "/v1/artifacts/{artifact_id}"): "resource",
    ("GET", "/v1/conversations/{conversation_id}"): "resource",
    ("PATCH", "/v1/conversations/{conversation_id}"): "resource",
    ("DELETE", "/v1/conversations/{conversation_id}"): "resource",
    ("GET", "/v1/conversations/{conversation_id}/messages"): "resource",
    ("POST", "/v1/conversations/{conversation_id}/messages"): "resource",
    ("GET", "/v1/conversations/{conversation_id}/events"): "resource",
    ("DELETE", "/v1/conversations/{conversation_id}/active-response"): "resource",
    ("POST", "/v1/conversations/{conversation_id}/turns/{turn_id}/approval"): "resource",
    ("POST", "/v1/conversations/{conversation_id}/turns/{turn_id}/input"): "resource",
    ("POST", "/v1/conversations/{conversation_id}/turns/{turn_id}/credentials"): "resource",
    ("POST", "/v1/conversations/{conversation_id}/turns/{turn_id}/steer"): "resource",
    ("PUT", "/v1/conversations/{conversation_id}/messages/{message_id}/feedback"): "resource",
    ("GET", "/v1/conversations/{conversation_id}/messages/{message_id}/feedback"): "resource",
    ("GET", "/v1/workflows/runs/{workflow_id}"): "resource",
    ("POST", "/v1/workflows/runs/{workflow_id}/approval"): "resource",
    ("POST", "/v1/workflows/runs/{workflow_id}/input"): "resource",
    ("GET", "/v1/tool-executions/{idempotency_key}"): "resource",
    ("POST", "/v1/tool-executions/{idempotency_key}/resolve"): "resource",
    ("POST", "/v1/tool-executions/{idempotency_key}/reconcile"): "resource",
    ("POST", "/v1/tool-executions/{idempotency_key}/compensate"): "resource",
    ("POST", "/v1/tool-executions/{idempotency_key}/resume"): "resource",
}

# Bodies that pass validation, so a 422 cannot hide whether the route checked the tenant.
BODIES: dict[tuple[str, str], Any] = {
    ("POST", "/v1/runs/{run_id}/clarification"): {"clarification": "yes"},
    ("POST", "/v1/runs/{run_id}/steer"): {"text": "stop"},
    ("POST", "/v1/runs/{run_id}/approval"): {"decision": "approved"},
    ("PATCH", "/v1/artifacts/{artifact_id}/status"): {"status": "quarantined"},
    ("PATCH", "/v1/conversations/{conversation_id}"): {"title": "taken"},
    ("POST", "/v1/conversations/{conversation_id}/messages"): {"text": "hello"},
    ("POST", "/v1/conversations/{conversation_id}/turns/{turn_id}/approval"): {
        "decision": "approved"
    },
    ("POST", "/v1/conversations/{conversation_id}/turns/{turn_id}/input"): {"text": "yes"},
    ("POST", "/v1/conversations/{conversation_id}/turns/{turn_id}/credentials"): {
        "credentials": {"x-api-key": "k"}
    },
    ("POST", "/v1/conversations/{conversation_id}/turns/{turn_id}/steer"): {"text": "stop"},
    ("PUT", "/v1/conversations/{conversation_id}/messages/{message_id}/feedback"): {
        "rating": "down"
    },
    ("POST", "/v1/workflows/runs/{workflow_id}/approval"): {"decision": "approved"},
    ("POST", "/v1/workflows/runs/{workflow_id}/input"): {"values": {"region": "EU"}},
    ("POST", "/v1/tool-executions/{idempotency_key}/resolve"): {
        "status": "failed",
        "reason": "taken",
    },
    ("POST", "/v1/tool-executions/{idempotency_key}/reconcile"): {},
    ("POST", "/v1/tool-executions/{idempotency_key}/compensate"): {"reason": "taken"},
    ("POST", "/v1/tool-executions/{idempotency_key}/resume"): {},
}

SECRET = "SECRET-PLAN-OF-TENANT-A"
TENANT_A = {"x-tenant-id": "tenant-a", "x-user-id": "alice", "x-scopes": "*"}
# Ids that only tenant A's resources have (the user, workflow and server names are shared).
A_IDS = (
    "run_id",
    "paused_run_id",
    "approval_id",
    "artifact_id",
    "conversation_id",
    "message_id",
    "workflow_id",
    "idempotency_key",
)
TENANT_B = {"x-tenant-id": "tenant-b", "x-user-id": "alice", "x-scopes": "*"}


def _settings() -> AppSettings:
    agent = {
        "version": "1.0.0",
        "description": "Works",
        "system_instructions": "Do the work.",
        "default_model": {"name": "d", "provider": "mock", "model": "deterministic"},
        "enabled_tools": ["mail.send"],
    }
    return AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
            "api": {"rate_limiting": {"enabled": False}},
            "agents": [{"name": "worker", **agent}],
            "workflows": {
                "report": {
                    "name": "report",
                    "version": "1.0.0",
                    "output_key": "report",
                    "nodes": [
                        {
                            "id": "work",
                            "kind": "agent",
                            "agent": "worker",
                            "output_key": "report",
                            "input_template": "{{input}}",
                        }
                    ],
                }
            },
        }
    )


def _model(request: ModelRequest) -> ModelResponse:
    """Asks to send mail when told to and no tool has answered yet; otherwise echoes."""
    asked = request.messages[-1]
    if "send mail" in asked.text_content and asked.role == Role.USER:
        return tool_call_response("mail.send", {"to": "bob", "body": SECRET})
    return ModelResponse(
        message=Message.text(Role.ASSISTANT, f"noted: {rendered_text(asked)}"),
        finish_reason=FinishReason.STOP,
        model="deterministic",
        provider="mock",
    )


def _send_tool() -> Tool:
    definition = ToolDefinition(
        name="mail.send",
        version="1",
        description="Send mail",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect=SideEffect.EXTERNAL,
        requires_approval=True,
    )
    return Tool(definition, lambda arguments, context: {"sent": SECRET})


async def _until(client: httpx.AsyncClient, path: str, done: set[str]) -> dict[str, Any]:
    for _ in range(500):
        body = (await client.get(path, headers=TENANT_A)).json()
        if body.get("status") in done:
            return dict(body)
        await asyncio.sleep(0.01)
    raise AssertionError(f"{path} did not reach {done}: {body}")


async def _tenant_a(app: Any, client: httpx.AsyncClient) -> dict[str, str]:
    """Tenant A's run, paused run with its approval, artifact, conversation and workflow run."""
    ids: dict[str, str] = {}
    created = await client.post(
        "/v1/runs", json={"agent": "worker", "input": SECRET}, headers=TENANT_A
    )
    assert created.status_code in (200, 201, 202), created.text
    ids["run_id"] = created.json()["run_id"]
    await _until(client, f"/v1/runs/{ids['run_id']}", {"completed"})

    paused = await client.post(
        "/v1/runs", json={"agent": "worker", "input": f"send mail {SECRET}"}, headers=TENANT_A
    )
    ids["paused_run_id"] = paused.json()["run_id"]
    await _until(client, f"/v1/runs/{ids['paused_run_id']}", {"awaiting_approval"})
    approvals = (await client.get("/v1/approvals", headers=TENANT_A)).json()
    ids["approval_id"] = next(a["id"] for a in approvals if a["run_id"] == ids["paused_run_id"])

    uploaded = await client.post(
        "/v1/artifacts", params={"name": "plan.txt"}, content=SECRET.encode(), headers=TENANT_A
    )
    assert uploaded.status_code in (200, 201), uploaded.text
    ids["artifact_id"] = uploaded.json()["id"]

    conversation = await client.post(
        "/v1/conversations", json={"agent": "worker", "title": SECRET}, headers=TENANT_A
    )
    ids["conversation_id"] = conversation.json()["id"]
    turn = await client.post(
        f"/v1/conversations/{ids['conversation_id']}/messages",
        json={"text": SECRET},
        headers=TENANT_A,
    )
    assert turn.status_code == 202, turn.text
    ids["turn_id"] = turn.json()["assistant_message_id"]
    for _ in range(500):
        messages = (
            await client.get(
                f"/v1/conversations/{ids['conversation_id']}/messages", headers=TENANT_A
            )
        ).json()
        if any(message["role"] == "assistant" for message in messages):
            break
        await asyncio.sleep(0.01)
    ids["message_id"] = next(m["id"] for m in messages if m["role"] == "assistant")

    workflow = await client.post(
        "/v1/workflows/report/runs", json={"input": SECRET}, headers=TENANT_A
    )
    assert workflow.status_code in (200, 201, 202), workflow.text
    ids["workflow_id"] = workflow.json()["workflow_id"]
    await _until(client, f"/v1/workflows/runs/{ids['workflow_id']}", {"completed"})

    # A sent mail, so the ledger holds one of tenant A's tool executions.
    sent = await client.post(
        "/v1/runs", json={"agent": "worker", "input": f"send mail {SECRET}"}, headers=TENANT_A
    )
    sent_id = sent.json()["run_id"]
    await _until(client, f"/v1/runs/{sent_id}", {"awaiting_approval"})
    decided = await client.post(
        f"/v1/runs/{sent_id}/approval", json={"decision": "approved"}, headers=TENANT_A
    )
    assert decided.status_code == 202, decided.text
    await _until(client, f"/v1/runs/{sent_id}", {"completed"})
    ledger = await client.get(
        "/v1/tool-executions", params={"status": "completed"}, headers=TENANT_A
    )
    ids["idempotency_key"] = ledger.json()["executions"][0]["idempotency_key"]
    ids["user_id"] = "alice"
    ids["name"] = "report"
    ids["server"] = "crm"
    return ids


def _path(template: str, ids: dict[str, str]) -> str:
    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "run_id" and template.endswith(("/approval", "/steer", "/clarification")):
            return ids.get("paused_run_id", ids["run_id"])
        return ids[name]

    return re.sub(r"\{(\w+)\}", value, template)


def _app() -> Any:
    settings = _settings()
    container = build_container(settings, additional_tools=[_send_tool()])
    container.runtime.router._providers["mock"] = MockModelProvider(handler=_model)
    return create_app(settings, container)


def test_every_route_is_classified() -> None:
    served = {
        (method, route.path)
        for route in _app().routes
        for method in getattr(route, "methods", None) or ()
        if method != "HEAD"
    }
    unlisted = sorted(served - ROUTES.keys())
    assert not unlisted, (
        "Classify each new route in ROUTES (public, platform, tenant or resource) and give it a "
        f"valid body in BODIES if it takes one: {unlisted}"
    )
    assert not sorted(ROUTES.keys() - served), "ROUTES lists routes the app no longer serves"


async def test_routes_that_are_not_public_need_an_identity() -> None:
    app = _app()
    ids = {name: "x" for name in re.findall(r"\{(\w+)\}", " ".join(p for _, p in ROUTES))}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for (method, template), scope in ROUTES.items():
            if scope == "public":
                continue
            response = await asyncio.wait_for(
                c.request(method, _path(template, ids), json=BODIES.get((method, template))),
                timeout=5,
            )
            assert response.status_code == 401, (method, template, response.status_code)


async def test_another_tenant_reaches_none_of_tenant_as_resources() -> None:
    app = _app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        ids = await _tenant_a(app, c)
        artifact_path = f"/v1/artifacts/{ids['artifact_id']}/metadata"
        before_artifact = (await c.get(artifact_path, headers=TENANT_A)).json()
        before_messages = (
            await c.get(f"/v1/conversations/{ids['conversation_id']}/messages", headers=TENANT_A)
        ).json()
        reached = []
        for (method, template), scope in ROUTES.items():
            if scope == "public":
                continue
            path = _path(template, ids)
            response = await asyncio.wait_for(
                c.request(method, path, json=BODIES.get((method, template)), headers=TENANT_B),
                timeout=5,
            )
            if SECRET in response.text:
                reached.append((method, template, response.status_code, "returned tenant A data"))
            if scope != "resource" and any(ids[key] in response.text for key in A_IDS):
                reached.append((method, template, response.status_code, "listed tenant A's ids"))
            if scope == "resource" and response.status_code not in (403, 404):
                reached.append((method, template, response.status_code, response.text[:200]))
        assert not reached, "\n".join(map(str, reached))

        # Tenant A's resources are as they were.
        run = (await c.get(f"/v1/runs/{ids['run_id']}", headers=TENANT_A)).json()
        assert run["status"] == "completed"
        paused = (await c.get(f"/v1/runs/{ids['paused_run_id']}", headers=TENANT_A)).json()
        assert paused["status"] == "awaiting_approval"
        approval = (await c.get(f"/v1/approvals/{ids['approval_id']}", headers=TENANT_A)).json()
        assert approval["status"] == "pending"
        assert (await c.get(artifact_path, headers=TENANT_A)).json() == before_artifact
        conversation = await c.get(f"/v1/conversations/{ids['conversation_id']}", headers=TENANT_A)
        assert conversation.json()["title"] == SECRET
        after_messages = (
            await c.get(f"/v1/conversations/{ids['conversation_id']}/messages", headers=TENANT_A)
        ).json()
        assert after_messages == before_messages
        workflow = await c.get(f"/v1/workflows/runs/{ids['workflow_id']}", headers=TENANT_A)
        assert workflow.json()["status"] == "completed"
