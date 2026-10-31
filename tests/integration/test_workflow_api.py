"""The configured workflows over HTTP: start a run, read it, answer its approval, be notified."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolDefinition
from algen_agent_runtime.workflows import service as workflow_service

HEADERS = {"x-tenant-id": "acme", "x-user-id": "hr-system", "x-scopes": "runs:read runs:write"}


def _settings(**extra: Any) -> AppSettings:
    return AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
            "agents": [
                {
                    "name": "screener",
                    "version": "1.0.0",
                    "description": "Screens",
                    "system_instructions": "Screen the application.",
                    "default_model": {"name": "d", "provider": "mock", "model": "deterministic"},
                }
            ],
            "workflows": {
                "screening": {
                    "name": "screening",
                    "version": "1.0.0",
                    "output_key": "screened",
                    "nodes": [
                        {
                            "id": "review",
                            "kind": "approval",
                            "output_key": "approval",
                            "approval": {"prompt": "Screen this application?"},
                        },
                        {
                            "id": "screen",
                            "kind": "agent",
                            "agent": "screener",
                            "depends_on": ["review"],
                            "output_key": "screened",
                            "input_template": "{{input}}",
                        },
                    ],
                },
                "untrusted": {
                    "name": "untrusted",
                    "version": "1.0.0",
                    "hook_provider": "somewhere.hooks:create_workflow_hooks",
                    "nodes": [
                        {"id": "go", "kind": "handler", "handler": "go", "output_key": "out"}
                    ],
                },
            },
            **extra,
        }
    )


async def _until(client: httpx.AsyncClient, workflow_id: str, status: str) -> dict[str, Any]:
    for _ in range(300):
        found = (await client.get(f"/v1/workflows/runs/{workflow_id}", headers=HEADERS)).json()
        if found["status"] == status:
            return found  # type: ignore[no-any-return]
        await asyncio.sleep(0.01)
    raise AssertionError(f"run never reached {status}: {found}")


@pytest.mark.anyio
async def test_another_system_starts_a_workflow_and_answers_its_approval() -> None:
    app = create_app(_settings())
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        listed = (await client.get("/v1/workflows", headers=HEADERS)).json()["workflows"]
        assert {item["name"]: item["available"] for item in listed} == {
            "screening": True,
            "untrusted": False,
        }
        started = await client.post(
            "/v1/workflows/screening/runs",
            json={"input": "Jane, Python, 6 years", "workflow_id": "application-0042"},
            headers=HEADERS,
        )
        assert started.status_code == 202
        waiting = await _until(client, "application-0042", "awaiting_approval")
        assert waiting["pause"]["prompt"] == "Screen this application?"
        # Starting again with the same id finds the run instead of starting a second one.
        again = await client.post(
            "/v1/workflows/screening/runs",
            json={"input": "Jane", "workflow_id": "application-0042"},
            headers=HEADERS,
        )
        assert again.json()["workflow_id"] == "application-0042"
        # Another tenant cannot see or answer it.
        other = {**HEADERS, "x-tenant-id": "other"}
        assert (
            await client.get("/v1/workflows/runs/application-0042", headers=other)
        ).status_code == 404
        decided = await client.post(
            "/v1/workflows/runs/application-0042/approval",
            json={"decision": "approved", "comment": "Go ahead"},
            headers=HEADERS,
        )
        assert decided.status_code == 202
        done = await _until(client, "application-0042", "completed")
        assert done["steps"] == {"review": "completed", "screen": "completed"}
        assert done["output"]


@pytest.mark.anyio
async def test_a_workflow_whose_code_is_not_trusted_cannot_start() -> None:
    app = create_app(_settings())
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        refused = await client.post("/v1/workflows/untrusted/runs", json={}, headers=HEADERS)
        assert refused.status_code >= 400
        assert "trusted_plugin_prefixes" in refused.text
        missing = await client.post("/v1/workflows/nope/runs", json={}, headers=HEADERS)
        assert missing.status_code == 404


@pytest.mark.anyio
async def test_a_waiting_run_is_notified_signed(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[httpx.Request] = []

    def client(**kwargs: Any) -> httpx.AsyncClient:
        def record(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(204)

        return httpx.AsyncClient(transport=httpx.MockTransport(record))

    monkeypatch.setattr(workflow_service, "create_safe_http_client", client)
    monkeypatch.setenv("HR_WEBHOOK_KEY", "shared-secret")
    settings = _settings(
        workflow_notifications={
            "webhook_url": "https://hr.example.com/hooks/algen",
            "signing_secret": "env://HR_WEBHOOK_KEY",
        }
    )
    app = create_app(settings)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        await http.post(
            "/v1/workflows/screening/runs",
            json={"input": "Jane", "workflow_id": "application-0043"},
            headers=HEADERS,
        )
        await _until(http, "application-0043", "awaiting_approval")
        for _ in range(100):
            if sent:
                break
            await asyncio.sleep(0.01)
    [request] = sent
    body = json.loads(request.content)
    assert (
        body["event"] == "workflow.awaiting_approval" and body["workflow_id"] == "application-0043"
    )
    expected = hmac.new(b"shared-secret", request.content, hashlib.sha256).hexdigest()
    assert request.headers["X-Algen-Signature"] == f"sha256={expected}"


def _tool_settings() -> AppSettings:
    return _settings(
        workflows={
            "evidence": {
                "name": "evidence",
                "version": "1.0.0",
                "output_key": "collected",
                "nodes": [
                    {
                        "id": "collect",
                        "kind": "handler",
                        "handler": "tool:evidence.collect",
                        "arguments": {"tag": "{{inputs.tag}}"},
                        "output_key": "collected",
                    }
                ],
            },
            "publish": {
                "name": "publish",
                "version": "1.0.0",
                "output_key": "published",
                "nodes": [
                    {
                        "id": "publish",
                        "kind": "handler",
                        "handler": "tool:release.publish",
                        "output_key": "published",
                    }
                ],
            },
        }
    )


def _register_tools(app: Any, published: list[Any]) -> None:
    tools = app.state.container.runtime.tools
    schema = {"type": "object"}
    tools.register(
        Tool(
            ToolDefinition(
                name="evidence.collect",
                version="1",
                description="Collect release evidence",
                input_schema=schema,
                output_schema=schema,
            ),
            lambda args, ctx: {"tag": args["tag"]},
        )
    )
    tools.register(
        Tool(
            ToolDefinition(
                name="release.publish",
                version="1",
                description="Publish the release",
                input_schema=schema,
                output_schema=schema,
                side_effect=SideEffect.EXTERNAL,
                requires_approval=True,
            ),
            lambda args, ctx: published.append(args) or {"published": True},
        )
    )


@pytest.mark.anyio
async def test_a_served_workflow_runs_a_tool_step() -> None:
    app = create_app(_tool_settings())
    _register_tools(app, [])
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        started = await client.post(
            "/v1/workflows/evidence/runs",
            json={"inputs": {"tag": "v1.2"}, "workflow_id": "evidence-1"},
            headers=HEADERS,
        )
        assert started.status_code == 202
        done = await _until(client, "evidence-1", "completed")
        assert done["steps"] == {"collect": "completed"}


@pytest.mark.anyio
async def test_a_tool_step_needing_approval_with_no_approval_node_cannot_start() -> None:
    published: list[Any] = []
    app = create_app(_tool_settings())
    _register_tools(app, published)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        refused = await client.post("/v1/workflows/publish/runs", json={}, headers=HEADERS)
        assert refused.status_code >= 400
        assert "requires approval" in refused.text
    assert published == []
