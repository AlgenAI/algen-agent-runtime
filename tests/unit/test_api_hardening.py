"""HTTP API hardening in 0.2.0b4: agent registration and version pinning, the agent UI, stdio
MCP environments, event-stream caps, JWT scopes and audience, artifact downloads and CORS."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import make_agent, make_runtime
from pydantic import ValidationError

from algen_agent_runtime.api.app import (
    _content_disposition,
    _HeldStreamingResponse,
    _StreamSlots,
    create_app,
)
from algen_agent_runtime.config.registry import InMemoryAgentRegistry, pinned_agent
from algen_agent_runtime.config.settings import ApiSettings, AppSettings
from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.mcp.client import MCPClientManager
from algen_agent_runtime.mcp.contracts import MCPStdioServerConfig
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.check import check_project
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolDefinition
from algen_agent_runtime.types.contracts import RunRequest, RunState, RunStatus
from algen_agent_runtime.ui.spec import extract_agent_manifest_spec
from algen_agent_runtime.ui.template import render_agent_ui

DEV = {"x-tenant-id": "t", "x-user-id": "u"}
XSS = "</ScRiPt><img src=x onerror=alert(1)>"


def _headers(scopes: str) -> dict[str, str]:
    return {**DEV, "x-scopes": scopes}


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# API-1: registration is a platform operation and never takes over another owner's agent.


def test_registry_refuses_a_version_of_another_owners_agent() -> None:
    registry = InMemoryAgentRegistry()
    registry.register(make_agent(name="support", version="1.0.0"))
    registry.register(make_agent(name="support", version="1.1.0"))  # the host may
    with pytest.raises(ConflictError, match="another owner"):
        registry.register(make_agent(name="support", version="99.0.0"), owner="http")
    registry.register(make_agent(name="helper", version="1.0.0"), owner="http")
    registry.register(make_agent(name="helper", version="2.0.0"), owner="http")
    with pytest.raises(ConflictError, match="another owner"):
        registry.register(make_agent(name="helper", version="3.0.0"))
    assert registry.get("support").version == "1.1.0"
    assert registry.owner("helper") == "http"
    assert registry.owner("support") is None


async def test_http_registration_needs_agents_admin_and_cannot_shadow_a_host_agent() -> None:
    app = create_app(AppSettings())
    container = app.state.container
    container.agents.register(make_agent(name="support", version="1.0.0"))
    async with _client(app) as client:
        body = make_agent(name="support", version="99.0.0").model_dump(mode="json")
        forbidden = await client.post("/v1/agents", json=body, headers=_headers("agents:write"))
        assert forbidden.status_code == 403
        assert "agents:admin" in forbidden.json()["detail"]
        shadow = await client.post("/v1/agents", json=body, headers=_headers("agents:admin"))
        assert shadow.status_code == 409
        new = make_agent(name="fresh", version="1.0.0").model_dump(mode="json")
        created = await client.post("/v1/agents", json=new, headers=_headers("agents:admin"))
        assert created.status_code == 201
        newer = make_agent(name="fresh", version="1.1.0").model_dump(mode="json")
        other_tenant = {"x-tenant-id": "other", "x-user-id": "admin", "x-scopes": "agents:admin"}
        hijack = await client.post("/v1/agents", json=newer, headers=other_tenant)
        assert hijack.status_code == 409
        same_tenant = await client.post("/v1/agents", json=newer, headers=_headers("agents:admin"))
        assert same_tenant.status_code == 201
    assert container.agents.get("support").version == "1.0.0"
    assert container.agents.owner("fresh") == "http:t"
    audits = await container.runtime.audits.list("t")
    assert any(a.action == "agent.registered" and a.resource_id == "fresh@1.0.0" for a in audits)


def _approval_tool() -> Tool:
    definition = ToolDefinition(
        name="mail.send",
        version="1",
        description="Send",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect=SideEffect.EXTERNAL,
        requires_approval=True,
    )
    return Tool(definition, lambda args, ctx: {"sent": True})


async def test_a_paused_run_resumes_with_the_version_it_started_with() -> None:
    provider = MockModelProvider([tool_call_response("mail.send", {"to": "bob"}), "done"])
    runtime = make_runtime(provider, make_agent(enabled_tools=frozenset({"mail.send"})))
    runtime.tools.register(_approval_tool())
    try:
        state = await runtime.start(
            RunRequest(agent="test-agent", input="x", tenant_id="t", user_id="u")
        )
        for _ in range(400):
            state = await runtime.status(state.id, "t")
            if state.status == RunStatus.AWAITING_APPROVAL:
                break
            await asyncio.sleep(0.005)
        assert state.agent_key == "test-agent@1.0.0"
        # A higher version registered while the run waits does not take it over.
        runtime.agents.register(
            make_agent(version="9.0.0", system_instructions="taken over", max_steps=1)
        )
        assert runtime._run_agent(state).version == "1.0.0"
        await runtime.resume(state.id, "t", {"decision": "approved"}, decided_by="u")
        done = await runtime.wait(state.id, "t")
        assert done.status == RunStatus.COMPLETED
    finally:
        await runtime.shutdown(grace_seconds=5)


def test_a_run_without_a_pin_or_with_a_removed_version_resolves_as_before() -> None:
    registry = InMemoryAgentRegistry()
    registry.register(make_agent(version="1.0.0"))
    registry.register(make_agent(version="2.0.0"))
    request = RunRequest(agent="test-agent", input="x", tenant_id="t", user_id="u")
    stored_by_b3 = RunState(request=request)  # agent_key None
    assert pinned_agent(registry, stored_by_b3) is None
    assert pinned_agent(registry, RunState(request=request, agent_key="test-agent@1.0.0"))
    removed = RunState(request=request, agent_key="test-agent@0.5.0")
    assert pinned_agent(registry, removed) is None
    runtime = make_runtime(agent=make_agent(version="1.0.0"))
    runtime.agents.register(make_agent(version="2.0.0"))
    assert runtime._run_agent(stored_by_b3).version == "2.0.0"
    assert runtime._run_agent(removed).version == "2.0.0"


# API-2: the agent UI is authenticated outside development and holds no settings dumps.


def _jwt_settings(monkeypatch: pytest.MonkeyPatch, **api: Any) -> AppSettings:
    monkeypatch.setenv("HARDENING_JWT_KEY", "a-test-signing-key-of-at-least-32-bytes!")
    return AppSettings.model_validate(
        {
            "api": {
                "auth_mode": "jwt",
                "jwt_key": "env://HARDENING_JWT_KEY",
                "jwt_algorithms": ["HS256"],
                "agent_ui_enabled": True,
                **api,
            }
        }
    )


def _token(scope: str) -> dict[str, str]:
    jwt = pytest.importorskip("jwt")
    token = jwt.encode(
        {"sub": "u", "tenant_id": "t", "scope": scope, "exp": 9_999_999_999},
        "a-test-signing-key-of-at-least-32-bytes!",
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


async def test_agent_ui_needs_agents_read_with_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("jwt")
    app = create_app(_jwt_settings(monkeypatch))
    async with _client(app) as client:
        for path in ("/agent-ui", "/agent-ui/", "/agent-ui/spec", "/ui"):
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers=_token("runs:read"))).status_code == 403
            allowed = await client.get(path, headers=_token("agents:read"))
            assert allowed.status_code == 200
            assert allowed.headers["cache-control"] == "no-store"


async def test_agent_ui_spec_holds_no_settings_dump() -> None:
    settings = AppSettings.model_validate(
        {
            "api": {"agent_ui_enabled": True},
            "telemetry": {"otlp_endpoint": "https://otel.example.com:4318"},
            "mcp_servers": [
                {
                    "name": "files",
                    "transport": "stdio",
                    "command": "files-mcp",
                    "env": {"FILES_TOKEN": "env://FILES_TOKEN", "FILES_ROOT": "/srv/files"},
                },
                {
                    "name": "remote",
                    "transport": "streamable_http",
                    "url": "https://user:pw@mcp.example.com/mcp?key=abc",
                },
            ],
        }
    )
    app = create_app(settings)
    async with _client(app) as client:
        spec = (await client.get("/agent-ui/spec")).json()
        page = (await client.get("/agent-ui")).text
    for key in ("security", "telemetry", "cache"):
        assert key not in spec
    assert "is not published" in spec["raw_yaml"]
    for leaked in ("otel.example.com", "/srv/files", "pw@", "key=abc"):
        assert leaked not in page
        assert leaked not in str(spec)
    remote = next(server for server in spec["mcp_servers"] if server["name"] == "remote")
    assert remote["url"] == "https://mcp.example.com/mcp"


def test_a_static_export_keeps_the_text_it_was_given() -> None:
    spec = extract_agent_manifest_spec(AppSettings(), raw_yaml="agents: []\n")
    assert spec["raw_yaml"] == "agents: []\n"


async def test_agent_ui_escapes_definitions_and_the_host() -> None:
    app = create_app(AppSettings.model_validate({"api": {"agent_ui_enabled": True}}))
    app.state.container.agents.register(make_agent(name="aaa", description=XSS))
    async with _client(app) as client:
        page = (await client.get("/agent-ui")).text
    assert "<img src=x" not in page
    assert "</ScRiPt>" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page  # __PAGE_DESC__, escaped

    rendered = render_agent_ui(
        {
            "title": "__API_ENDPOINT__",
            "description": "<b>d</b>",
            "api_endpoint": '"><script>alert(1)</script>',
            "agents": [{"description": XSS}],
        }
    )
    assert "<script>alert(1)" not in rendered
    assert "&quot;&gt;&lt;script&gt;" in rendered
    assert "<b>d</b>" not in rendered
    # Placeholders are substituted once: a value naming one is shown as text.
    assert "__API_ENDPOINT__" in rendered


# MCP stdio env: credentials only as env:// references, resolved at start.


def test_stdio_env_refuses_a_literal_credential() -> None:
    with pytest.raises(ValidationError, match="env:// reference"):
        MCPStdioServerConfig(name="gh", command="gh-mcp", env={"GITHUB_TOKEN": "ghp_literal"})
    with pytest.raises(ValidationError, match="names no environment variable"):
        MCPStdioServerConfig(name="gh", command="gh-mcp", env={"GITHUB_TOKEN": "env://"})
    config = MCPStdioServerConfig(
        name="gh",
        command="gh-mcp",
        env={"GITHUB_TOKEN": "env://GH_TOKEN", "MAX_TOKENS": "100", "LOG_LEVEL": "debug"},
    )
    assert config.env["MAX_TOKENS"] == "100"


async def test_stdio_env_references_resolve_before_the_server_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "mcp", None)
    seen: dict[str, Any] = {}

    def capture(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs["explicit_env"])
        raise RuntimeError("stop before starting")

    monkeypatch.setattr("algen_agent_runtime.mcp.client.prepare_command", capture)
    manager = MCPClientManager(
        (
            MCPStdioServerConfig(
                name="gh",
                command="gh-mcp",
                env={"GITHUB_TOKEN": "env://GH_TOKEN", "LOG_LEVEL": "debug"},
            ),
        ),
        environment={"GH_TOKEN": "resolved-value"},
    )
    with pytest.raises(Exception):  # noqa: B017 - the stub stops the connection
        await manager.connect("gh")
    assert seen == {"GITHUB_TOKEN": "resolved-value", "LOG_LEVEL": "debug"}


# API-5: a principal holds at most api.max_streams_per_principal event streams.


def test_stream_slots_cap_each_principal_and_release_once() -> None:
    slots = _StreamSlots(2)
    first = slots.acquire("t", "u")
    slots.acquire("t", "u")
    with pytest.raises(Exception) as refused:
        slots.acquire("t", "u")
    assert getattr(refused.value, "status_code", None) == 429
    slots.acquire("t", "other")  # another principal has its own slots
    first()
    first()  # releasing twice frees one slot only
    assert slots.held("t", "u") == 1
    slots.acquire("t", "u")


async def test_a_stream_releases_its_slot_when_the_client_goes_away() -> None:
    released: list[bool] = []

    async def body() -> AsyncIterator[bytes]:
        yield b": keepalive\n\n"
        await asyncio.sleep(3600)
        yield b""

    response = _HeldStreamingResponse(body(), lambda: released.append(True))

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            raise OSError("client gone")

    scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
    with pytest.raises(Exception):  # noqa: B017 - starlette reports the disconnect
        await response(scope, receive, send)
    assert released == [True]


async def test_event_streams_answer_429_past_the_cap() -> None:
    settings = AppSettings.model_validate({"api": {"max_streams_per_principal": 1}})
    app = create_app(settings)
    async with _client(app) as client:
        created = await client.post(
            "/v1/conversations",
            json={"agent": "test-agent"},
            headers=_headers("conversations:write conversations:read"),
        )
        assert created.status_code == 201
        conversation = created.json()["id"]
        app.state.stream_slots.acquire("t", "u")  # one stream already open
        refused = await client.get(
            f"/v1/conversations/{conversation}/events", headers=_headers("conversations:read")
        )
        assert refused.status_code == 429
        assert refused.headers["retry-after"] == "5"


# API-7: Last-Event-ID, Content-Disposition, nosniff, CORS.


async def test_a_bad_last_event_id_is_a_400() -> None:
    app = create_app(AppSettings())
    async with _client(app) as client:
        created = await client.post(
            "/v1/conversations",
            json={"agent": "test-agent"},
            headers=_headers("conversations:write"),
        )
        conversation = created.json()["id"]
        for value in ("abc", "-1", "1e3", "99999999999999999999"):
            response = await client.get(
                f"/v1/conversations/{conversation}/events",
                headers={**_headers("conversations:read"), "Last-Event-ID": value},
            )
            assert response.status_code == 400, value
        assert app.state.stream_slots.held("t", "u") == 0


def test_content_disposition_quotes_nothing_it_should_not() -> None:
    header = _content_disposition('evil"; filename="x.html')
    assert header.startswith('attachment; filename="evil__ filename=_x.html"; ')
    assert header.endswith("filename*=UTF-8''evil%22%3B%20filename%3D%22x.html")
    assert _content_disposition("résumé/2026.pdf") == (
        "attachment; filename=\"r_sum_/2026.pdf\"; filename*=UTF-8''r%C3%A9sum%C3%A9%2F2026.pdf"
    )


async def test_artifact_download_headers() -> None:
    app = create_app(AppSettings.model_validate({"security": {"upload_scanner": "basic"}}))
    async with _client(app) as client:
        uploaded = await client.post(
            "/v1/artifacts",
            params={"name": 'a"b.txt', "media_type": "text/plain"},
            content=b"hello",
            headers=_headers("artifacts:write"),
        )
        assert uploaded.status_code == 201, uploaded.text
        download = await client.get(
            f"/v1/artifacts/{uploaded.json()['id']}", headers=_headers("artifacts:read")
        )
    assert download.status_code == 200
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["content-disposition"] == (
        "attachment; filename=\"a_b.txt\"; filename*=UTF-8''a%22b.txt"
    )


def test_cors_wildcard_with_credentials_is_refused() -> None:
    with pytest.raises(ValidationError, match="cors_allow_credentials"):
        ApiSettings(cors_allowed_origins=("*",), cors_allow_credentials=True)
    ApiSettings(cors_allowed_origins=("*",))
    ApiSettings(cors_allowed_origins=("https://app.example.com",), cors_allow_credentials=True)


# API-6: static JWT never grants "*", and a missing audience is reported.


async def test_static_jwt_never_takes_star_from_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("jwt")
    app = create_app(_jwt_settings(monkeypatch))
    async with _client(app) as client:
        assert (await client.get("/v1/agents", headers=_token("*"))).status_code == 403
        assert (await client.get("/v1/agents", headers=_token("* agents:read"))).status_code == 200


def test_jwt_audience_warning() -> None:
    rsa = ApiSettings(auth_mode="jwt", jwt_key="env://K")
    assert rsa.jwt_audience_warning is not None
    assert "RS256" in rsa.jwt_audience_warning
    assert (
        ApiSettings(auth_mode="jwt", jwt_key="env://K", jwt_audience="rt").jwt_audience_warning
        is None
    )
    assert (
        ApiSettings(auth_mode="jwt", jwt_key="env://K", jwt_algorithms=("HS256",))
    ).jwt_audience_warning is None
    assert ApiSettings().jwt_audience_warning is None


def test_jwt_audience_warning_is_logged_and_checked(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    pytest.importorskip("jwt")
    from algen_agent_runtime.auth.authenticator import ApiAuthenticator

    monkeypatch.setenv("HARDENING_PUBLIC_KEY", "not-used-before-a-request")
    with caplog.at_level(logging.WARNING, logger="algen_agent_runtime.auth.authenticator"):
        ApiAuthenticator(ApiSettings(auth_mode="jwt", jwt_key="env://HARDENING_PUBLIC_KEY"))
    assert "jwt_audience is not set" in caplog.text

    config = tmp_path / "agent.yaml"
    config.write_text("api:\n  auth_mode: jwt\n  jwt_key: env://HARDENING_PUBLIC_KEY\n")
    report = check_project([config], profile="standalone")
    codes = {issue.code: issue for issue in report.issues}
    assert codes["JWT_AUDIENCE_MISSING"].severity == "warning"
    assert codes["JWT_AUDIENCE_MISSING"].field == "api.jwt_audience"
