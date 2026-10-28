"""OAuth for remote MCP servers: flow, storage, refresh, revocation and the not-connected error.

The authorization server is an ``httpx.MockTransport`` and MCP sessions are fakes, so nothing
here touches the network or needs the ``mcp`` extra. Tests that encrypt need ``cryptography``
(the ``encryption`` extra) and skip without it.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import make_agent, make_runtime
from pydantic import SecretStr, ValidationError

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.config.settings import AppSettings, StorageSettings
from algen_agent_runtime.exceptions.errors import (
    ConfigurationError,
    MCPNotConnectedError,
    PolicyDeniedError,
    ToolExecutionError,
)
from algen_agent_runtime.mcp.client import MCPClientManager
from algen_agent_runtime.mcp.contracts import (
    MCPStreamableHttpServerConfig,
    OAuthClientAuth,
)
from algen_agent_runtime.mcp.oauth import (
    MCPOAuthError,
    MCPOAuthManager,
    MCPOAuthStateError,
    StoredTokenAuth,
    build_mcp_oauth_manager,
)
from algen_agent_runtime.mcp.tokens import (
    InMemoryMCPTokenStore,
    MCPOAuthTokenSet,
    MCPTokenKey,
    PostgresMCPTokenStore,
)
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.orchestration.describe import describe_components
from algen_agent_runtime.tools.testing import fake_tool_context
from algen_agent_runtime.types.contracts import ApprovalPolicy, RunRequest, RunStatus

MCP_URL = "https://mcp.example.com/mcp"
ISSUER = "https://auth.example.com"
REDIRECT = "https://runtime.example.com/v1/mcp/oauth/callback"
ENV = {"SLACK_CLIENT_SECRET": "client-secret-value"}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeAuthorizationServer:
    """RFC 9728 / 8414 metadata, an authorize step, a token endpoint and RFC 7009 revocation."""

    def __init__(self, *, revocation: bool = True, metadata: bool = True) -> None:
        self.revocation = revocation
        self.metadata = metadata
        self.codes: dict[str, dict[str, str]] = {}
        self.refresh_tokens: dict[str, int] = {}
        self.revoked: list[dict[str, str]] = []
        self.token_requests: list[dict[str, str]] = []
        self.issued = 0
        self.fail_refresh_with: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://mcp.example.com/.well-known/oauth-protected-resource/mcp":
            if not self.metadata:
                return httpx.Response(404)
            return httpx.Response(
                200, json={"resource": MCP_URL, "authorization_servers": [ISSUER]}
            )
        if url == f"{ISSUER}/.well-known/oauth-authorization-server":
            body: dict[str, Any] = {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token",
                "code_challenge_methods_supported": ["S256"],
            }
            if self.revocation:
                body["revocation_endpoint"] = f"{ISSUER}/revoke"
            return httpx.Response(200, json=body)
        if url == f"{ISSUER}/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            return self._token(form)
        if url == f"{ISSUER}/revoke":
            self.revoked.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            return httpx.Response(200)
        return httpx.Response(404)

    def consent(self, authorize_url: str) -> tuple[str, str]:
        """The user approves: returns (code, state) as the provider's redirect would."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        code = f"code-{len(self.codes)}"
        self.codes[code] = query
        return code, query["state"]

    def _issue(self) -> dict[str, Any]:
        self.issued += 1
        refresh = f"refresh-secret-{self.issued}"
        self.refresh_tokens[refresh] = self.issued
        return {
            "access_token": f"access-secret-{self.issued}",
            "refresh_token": refresh,
            "token_type": "bearer",
            "expires_in": 3600,
            "scope": "channels:read chat:write",
        }

    def _token(self, form: dict[str, str]) -> httpx.Response:
        if form.get("client_secret") != ENV["SLACK_CLIENT_SECRET"]:
            return httpx.Response(401, json={"error": "invalid_client"})
        if form["grant_type"] == "authorization_code":
            issued = self.codes.pop(form.get("code", ""), None)
            if issued is None or issued["redirect_uri"] != form.get("redirect_uri"):
                return httpx.Response(400, json={"error": "invalid_grant"})
            verifier = form.get("code_verifier", "")
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            if challenge != issued["code_challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self._issue())
        if form["grant_type"] == "refresh_token":
            if self.fail_refresh_with is not None:
                return httpx.Response(self.fail_refresh_with, json={"error": "server_error"})
            if self.refresh_tokens.pop(form.get("refresh_token", ""), None) is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self._issue())
        return httpx.Response(400, json={"error": "unsupported_grant_type"})


def server_config(**auth: Any) -> MCPStreamableHttpServerConfig:
    return MCPStreamableHttpServerConfig(
        name="slack",
        url=MCP_URL,
        auth=OAuthClientAuth(
            **{
                "client_id": "slack-client",
                "client_secret_ref": "env://SLACK_CLIENT_SECRET",
                "scopes": ("channels:read", "chat:write"),
                "redirect_uri": REDIRECT,
                **auth,
            }
        ),
    )


def make_manager(
    provider: FakeAuthorizationServer | None = None,
    *,
    clock: Clock | None = None,
    store: Any = None,
    **auth: Any,
) -> tuple[MCPOAuthManager, FakeAuthorizationServer]:
    provider = provider or FakeAuthorizationServer()
    manager = MCPOAuthManager(
        [server_config(**auth)],
        store or InMemoryMCPTokenStore(),
        environment=ENV,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider.handler)),
        clock=clock or Clock(),
    )
    return manager, provider


async def connect(
    manager: MCPOAuthManager, provider: FakeAuthorizationServer, tenant: str, user: str
) -> None:
    started = await manager.start("slack", tenant, user)
    code, state = provider.consent(started.authorize_url)
    await manager.complete(state, code)


class FakeSession:
    def __init__(self, tokens: list[str | None]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tokens = tokens

    async def initialize(self) -> None:
        pass

    async def list_tools(self, cursor: str | None = None) -> Any:
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="post_message",
                    description="Post a Slack message",
                    inputSchema={"type": "object", "properties": {"text": {"type": "string"}}},
                    outputSchema=None,
                    annotations=None,
                )
            ]
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text="posted")])


def client_manager(manager: MCPOAuthManager) -> tuple[MCPClientManager, list[Any]]:
    """An MCPClientManager whose sessions are fakes; records each session's auth."""
    opened: list[Any] = []

    async def session_factory(config: Any, stack: Any, *, auth: Any) -> FakeSession:
        session = FakeSession([])
        opened.append((auth, session))
        return session

    mcp = MCPClientManager(
        [server_config()], environment=ENV, session_factory=session_factory, oauth=manager
    )
    return mcp, opened


# -- configuration ------------------------------------------------------------------------------


def test_old_configs_without_auth_are_unchanged() -> None:
    config = MCPStreamableHttpServerConfig(
        name="inventory", url=MCP_URL, auth_token_ref="env://MCP_INVENTORY_TOKEN"
    )
    assert config.auth is None
    settings = AppSettings.model_validate(
        {"mcp_servers": [config.model_dump(mode="json", exclude={"auth"})]}
    )
    assert settings.mcp_servers[0] == config
    assert settings.storage.mcp_oauth_token_store == "memory"
    container = build_container(settings)
    assert container.mcp is not None and container.mcp.oauth is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"client_secret_ref": "literal-secret"}, "env://"),
        ({"redirect_uri": "http://runtime.example.com/cb"}, "https"),
        ({"redirect_uri": "https://runtime.example.com/cb#frag"}, "fragment"),
        ({"authorize_params": {"state": "fixed"}}, "authorize_params"),
        ({"authorization_endpoint": f"{ISSUER}/authorize"}, "together"),
        ({"client_id": None}, "exactly one"),
        ({"discovery_user_id": "u1"}, "token_scope 'user'"),
        ({"token_scope": "user", "discovery_tenant_id": "t1"}, "discovery_user_id"),
        ({"scopes": ("two words",)}, "whitespace"),
        ({"surprise": True}, "Extra inputs"),
    ],
)
def test_oauth_config_rejects_unsafe_values(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        server_config(**change)


def test_oauth_cannot_be_mixed_with_static_tokens() -> None:
    auth = {"client_id": "c", "redirect_uri": REDIRECT}
    with pytest.raises(ValidationError, match="not both"):
        MCPStreamableHttpServerConfig(
            name="slack", url=MCP_URL, auth=auth, auth_token_ref="env://TOKEN"
        )
    with pytest.raises(ValidationError, match="Authorization"):
        MCPStreamableHttpServerConfig(
            name="slack", url=MCP_URL, auth=auth, secret_headers={"Authorization": "env://T"}
        )


def test_postgres_token_store_requires_an_encryption_key() -> None:
    with pytest.raises(ValidationError, match="mcp_oauth_encryption_key"):
        StorageSettings(mcp_oauth_token_store="postgres", postgres_dsn="env://DSN")
    with pytest.raises(ValidationError, match="env://"):
        StorageSettings(mcp_oauth_encryption_key="raw-key")


YAML = """
storage:
  mcp_oauth_token_store: memory
mcp_servers:
  - name: linear
    transport: streamable_http
    url: https://mcp.linear.app/mcp
    auth:
      type: oauth
      client_id_ref: env://LINEAR_CLIENT_ID
      client_secret_ref: env://LINEAR_CLIENT_SECRET
      scopes: [read, write]
      token_scope: user
      redirect_uri: https://runtime.example.com/v1/mcp/oauth/callback
"""


def test_check_accepts_oauth_yaml_and_describe_shows_it_without_secrets(tmp_path: Path) -> None:
    from algen_agent_runtime.orchestration.check import check_project

    path = tmp_path / "agent.yaml"
    path.write_text(YAML, encoding="utf-8")
    report = check_project([path])
    assert report.ok, report.issues
    settings = AppSettings.model_validate(
        {
            "storage": {"mcp_oauth_token_store": "memory"},
            "mcp_servers": [server_config(token_scope="user").model_dump(mode="json")],
        }
    )
    described = describe_components(settings).mcp_servers[0]
    assert described.auth is not None
    assert described.auth.token_scope == "user"
    assert described.auth.client == "confidential"
    assert described.env == ("SLACK_CLIENT_SECRET",)
    assert ENV["SLACK_CLIENT_SECRET"] not in described.model_dump_json()


# -- the flow ------------------------------------------------------------------------------------


async def test_happy_path_authorizes_with_pkce_and_stores_the_tokens() -> None:
    manager, provider = make_manager()
    started = await manager.start("slack", "tenant-a", "alice")
    query = {k: v[0] for k, v in parse_qs(urlsplit(started.authorize_url).query).items()}
    assert started.authorize_url.startswith(f"{ISSUER}/authorize?")
    assert query["response_type"] == "code"
    assert query["client_id"] == "slack-client"
    assert query["redirect_uri"] == REDIRECT
    assert query["code_challenge_method"] == "S256"
    assert query["scope"] == "channels:read chat:write"
    assert query["resource"] == MCP_URL
    assert len(query["state"]) >= 43
    assert "client_secret" not in started.authorize_url

    code, state = provider.consent(started.authorize_url)
    completion = await manager.complete(state, code)
    assert completion.key == MCPTokenKey("tenant-a", None, "slack")
    assert completion.status.connected and completion.status.refreshable
    exchange = provider.token_requests[0]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["redirect_uri"] == REDIRECT
    assert exchange["resource"] == MCP_URL
    assert await manager.access_token("slack", "tenant-a", "bob") == "access-secret-1"


async def test_explicit_endpoints_and_public_clients_skip_discovery_and_secret() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert "client_secret" not in form and form["client_id"] == "public-client"
        return httpx.Response(200, json={"access_token": "public-access", "token_type": "Bearer"})

    config = MCPStreamableHttpServerConfig(
        name="slack",
        url=MCP_URL,
        auth=OAuthClientAuth(
            client_id="public-client",
            redirect_uri=REDIRECT,
            authorization_endpoint="https://slack.example.com/oauth/authorize",
            token_endpoint="https://slack.example.com/oauth/token",
            send_resource=False,
        ),
    )
    manager = MCPOAuthManager(
        [config],
        InMemoryMCPTokenStore(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    started = await manager.start("slack", "tenant-a", "alice")
    assert "resource=" not in started.authorize_url
    state = parse_qs(urlsplit(started.authorize_url).query)["state"][0]
    await manager.complete(state, "code")
    assert seen == ["https://slack.example.com/oauth/token"]
    assert await manager.access_token("slack", "tenant-a", "alice") == "public-access"


async def test_discovery_rejects_metadata_for_another_issuer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth-protected-resource" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, json={"issuer": "https://evil.example.com"})

    manager = MCPOAuthManager(
        [server_config()],
        InMemoryMCPTokenStore(),
        environment=ENV,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(MCPOAuthError, match="different issuer"):
        await manager.start("slack", "tenant-a", "alice")


async def test_state_replay_unknown_state_and_expiry_are_rejected() -> None:
    clock = Clock()
    manager, provider = make_manager(clock=clock)
    started = await manager.start("slack", "tenant-a", "alice")
    code, state = provider.consent(started.authorize_url)
    await manager.complete(state, code)
    # Replaying the provider's redirect finds no state: it was used once and deleted.
    with pytest.raises(MCPOAuthStateError, match="already used"):
        await manager.complete(state, code)
    # A forged callback (CSRF) has a state the Runtime never issued, or none.
    with pytest.raises(MCPOAuthStateError):
        await manager.complete("forged-state", "attacker-code")
    with pytest.raises(MCPOAuthStateError):
        await manager.complete(None, "attacker-code")
    started = await manager.start("slack", "tenant-a", "alice")
    code, state = provider.consent(started.authorize_url)
    clock.advance(601)
    with pytest.raises(MCPOAuthStateError, match="expired"):
        await manager.complete(state, code)
    # The provider's own refusal consumes the state and stores nothing.
    started = await manager.start("slack", "tenant-b", "carol")
    _, state = provider.consent(started.authorize_url)
    with pytest.raises(MCPOAuthError, match="access_denied") as refused:
        await manager.complete(state, None, "access_denied")
    assert refused.value.server == "slack"
    assert not (await manager.status("slack", "tenant-b", "carol")).connected


async def test_tokens_are_isolated_per_tenant() -> None:
    manager, provider = make_manager()
    await connect(manager, provider, "tenant-a", "alice")
    assert (await manager.status("slack", "tenant-a", "alice")).connected
    assert not (await manager.status("slack", "tenant-b", "alice")).connected
    with pytest.raises(MCPNotConnectedError) as missing:
        await manager.access_token("slack", "tenant-b", "alice")
    assert missing.value.details() == {
        "code": "mcp_not_connected",
        "server": "slack",
        "token_scope": "tenant",
        "start_path": "/v1/mcp/servers/slack/oauth/start",
    }
    # Revoking in tenant B cannot touch tenant A's connection.
    assert not (await manager.revoke("slack", "tenant-b", "alice")).removed
    assert (await manager.status("slack", "tenant-a", "alice")).connected


async def test_user_scope_keeps_one_connection_per_user_and_fails_closed_without_user() -> None:
    manager, provider = make_manager(token_scope="user")
    await connect(manager, provider, "tenant-a", "alice")
    assert await manager.access_token("slack", "tenant-a", "alice") == "access-secret-1"
    with pytest.raises(MCPNotConnectedError) as missing:
        await manager.access_token("slack", "tenant-a", "bob")
    assert missing.value.token_scope == "user"
    # No user means no connection at all, never a fallback to someone else's or the tenant's.
    for user in ("", "   ", None):
        with pytest.raises(PolicyDeniedError, match="no user_id"):
            await manager.access_token("slack", "tenant-a", user)
    mcp, opened = client_manager(manager)
    await mcp.discover_tools("slack", tenant_id="tenant-a", user_id="alice")
    tool = mcp._discovered_tools["mcp.slack.post_message"]
    with pytest.raises(PolicyDeniedError):
        await tool.execute(
            {"text": "hi"}, fake_tool_context(tool, tenant_id="tenant-a", user_id="")
        )
    assert len(opened) == 1


async def test_refresh_on_expiry_rotates_tokens() -> None:
    clock = Clock()
    manager, provider = make_manager(clock=clock)
    await connect(manager, provider, "tenant-a", "alice")
    clock.advance(3600 - 30)  # inside the 60-second refresh margin
    assert await manager.access_token("slack", "tenant-a", "alice") == "access-secret-2"
    assert provider.token_requests[-1]["grant_type"] == "refresh_token"
    assert provider.token_requests[-1]["refresh_token"] == "refresh-secret-1"
    status = await manager.status("slack", "tenant-a", "alice")
    assert status.expires_at == clock.now + timedelta(seconds=3600)
    # A provider outage keeps the token for a retry; a dead grant disconnects.
    clock.advance(3600)
    provider.fail_refresh_with = 503
    with pytest.raises(ToolExecutionError, match="HTTP 503"):
        await manager.access_token("slack", "tenant-a", "alice")
    assert (await manager.status("slack", "tenant-a", "alice")).connected
    provider.fail_refresh_with = None
    provider.refresh_tokens.clear()
    with pytest.raises(MCPNotConnectedError):
        await manager.access_token("slack", "tenant-a", "alice")
    assert not (await manager.status("slack", "tenant-a", "alice")).connected


async def test_a_refresh_lost_to_another_worker_keeps_the_winners_token() -> None:
    clock = Clock()
    store = InMemoryMCPTokenStore()
    provider = FakeAuthorizationServer()
    key = MCPTokenKey("tenant-a", None, "slack")
    winner = MCPOAuthTokenSet(
        access_token=SecretStr("access-secret-winner"),
        refresh_token=SecretStr("refresh-secret-winner"),
        expires_at=clock.now + timedelta(hours=2),
        connected_at=clock.now,
        connected_by="alice",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/token") and b"refresh_token" in request.content:
            # Another worker refreshed first: the provider already rotated the refresh token.
            await store.put(key, winner)
            return httpx.Response(400, json={"error": "invalid_grant"})
        return provider.handler(request)

    manager = MCPOAuthManager(
        [server_config()],
        store,
        environment=ENV,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=clock,
    )
    await connect(manager, provider, "tenant-a", "alice")
    clock.advance(3600)
    assert await manager.access_token("slack", "tenant-a", "alice") == "access-secret-winner"
    assert (await manager.status("slack", "tenant-a", "alice")).connected


async def test_transport_auth_sends_the_token_and_refreshes_once_on_401() -> None:
    manager, provider = make_manager()
    await connect(manager, provider, "tenant-a", "alice")
    seen: list[str] = []

    def mcp_server(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        # The server has revoked the first token on its side.
        if request.headers.get("authorization") == "Bearer access-secret-1":
            return httpx.Response(401)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(mcp_server),
        auth=StoredTokenAuth(manager, "slack", "tenant-a", None),
    ) as client:
        response = await client.post(MCP_URL, json={"jsonrpc": "2.0"})
    assert response.status_code == 200
    assert seen == ["Bearer access-secret-1", "Bearer access-secret-2"]
    assert "access-secret" not in repr(StoredTokenAuth(manager, "slack", "tenant-a", None))


async def test_not_connected_reaches_the_model_as_a_clean_tool_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    manager, provider = make_manager()
    await connect(manager, provider, "tenant-a", "alice")
    mcp, _ = client_manager(manager)
    tools = await mcp.discover_tools("slack", tenant_id="tenant-a")
    agent = make_agent(
        enabled_tools={"mcp.slack.post_message"},
        tool_permissions={"mcp.slack"},
        approval_policy=ApprovalPolicy(require_for_side_effects=False),
    )
    runtime = make_runtime(
        MockModelProvider(
            [tool_call_response("mcp.slack.post_message", {"text": "hi"}), "Please connect Slack."]
        ),
        agent,
    )
    for tool in tools:
        runtime.tools.register(tool)
    # Tenant B never connected; the default on_error ("fail_run") still lets the model answer.
    result = await runtime.run(
        RunRequest(agent="test-agent", input="post hi", tenant_id="tenant-b", user_id="bob")
    )
    assert result.status == RunStatus.COMPLETED
    assert result.output == "Please connect Slack."
    state = await runtime.runs.get(result.run_id, "tenant-b")
    assert state is not None
    tool_message = next(m for m in state.messages if m.tool_call_id)
    payload = json.loads(tool_message.content[0].text)
    assert payload["error"]["type"] == "MCPNotConnected"
    assert payload["error"]["server"] == "slack"
    events = await runtime.events.history(state.id)
    required = [event for event in events if event.type == "mcp.auth_required"]
    assert required[0].data == {
        "code": "mcp_not_connected",
        "server": "slack",
        "token_scope": "tenant",
        "start_path": "/v1/mcp/servers/slack/oauth/start",
    }
    audits = await runtime.audits.list("tenant-b")
    assert any(audit.outcome == "not_connected" for audit in audits)

    # Tenant A's call runs on tenant A's own connection.
    runtime_a = make_runtime(
        MockModelProvider([tool_call_response("mcp.slack.post_message", {"text": "hi"}), "Done"]),
        agent,
    )
    for tool in tools:
        runtime_a.tools.register(tool)
    done = await runtime_a.run(
        RunRequest(agent="test-agent", input="post hi", tenant_id="tenant-a", user_id="alice")
    )
    assert done.status == RunStatus.COMPLETED
    recorded = " ".join(
        [
            caplog.text,
            *(e.model_dump_json() for e in await runtime.events.history(state.id)),
            *(e.model_dump_json() for e in await runtime_a.events.history(done.run_id)),
            *(a.model_dump_json() for a in await runtime_a.audits.list("tenant-a")),
            *(a.model_dump_json() for a in audits),
            done.model_dump_json(),
            state.model_dump_json(),
            str(await runtime_a.runs.get(done.run_id, "tenant-a")),
        ]
    )
    assert "access-secret" not in recorded
    assert "refresh-secret" not in recorded


async def test_revoke_removes_the_token_calls_the_provider_and_closes_sessions() -> None:
    manager, provider = make_manager()
    await connect(manager, provider, "tenant-a", "alice")
    mcp, _ = client_manager(manager)
    await mcp.connect("slack", tenant_id="tenant-a", user_id="alice")
    assert len(mcp._oauth_connections) == 1
    outcome = await manager.revoke("slack", "tenant-a", "alice")
    assert outcome.removed and outcome.provider_revoked is True
    assert provider.revoked == [
        {
            "token": "refresh-secret-1",
            "token_type_hint": "refresh_token",
            "client_id": "slack-client",
            "client_secret": ENV["SLACK_CLIENT_SECRET"],
        }
    ]
    assert mcp._oauth_connections == {}
    with pytest.raises(MCPNotConnectedError):
        await mcp.connect("slack", tenant_id="tenant-a", user_id="alice")

    quiet, quiet_provider = make_manager(FakeAuthorizationServer(revocation=False))
    await connect(quiet, quiet_provider, "tenant-a", "alice")
    outcome = await quiet.revoke("slack", "tenant-a", "alice")
    assert outcome.removed and outcome.provider_revoked is None


async def test_startup_discovery_waits_for_a_connection_or_uses_the_discovery_principal() -> None:
    manager, _ = make_manager()
    mcp, _ = client_manager(manager)
    with pytest.raises(MCPNotConnectedError):
        await mcp.discover_tools("slack")
    principal, principal_provider = make_manager(discovery_tenant_id="ops")
    await connect(principal, principal_provider, "ops", "admin")
    mcp, _ = client_manager(principal)
    tools = await mcp.discover_tools("slack")
    assert [tool.definition.name for tool in tools] == ["mcp.slack.post_message"]
    assert "slack" in mcp.discovered_servers
    # Listing again through another tenant adds nothing.
    assert await mcp.discover_tools("slack", tenant_id="ops") == ()


def test_token_sets_never_print_their_secrets() -> None:
    tokens = MCPOAuthTokenSet(
        access_token=SecretStr("access-secret-x"),
        refresh_token=SecretStr("refresh-secret-x"),
        connected_at=datetime.now(UTC),
        connected_by="alice",
    )
    assert "access-secret" not in repr(tokens) + str(tokens) + tokens.model_dump_json()


# -- missing extras -----------------------------------------------------------------------------


async def test_missing_mcp_extra_is_actionable_after_secrets_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, provider = make_manager()
    await connect(manager, provider, "tenant-a", "alice")
    monkeypatch.setitem(sys.modules, "mcp", None)
    with_header = MCPStreamableHttpServerConfig(
        name="slack",
        url=MCP_URL,
        secret_headers={"X-Workspace": "env://MISSING_WORKSPACE_HEADER"},
        auth=server_config().auth,
    )
    mcp = MCPClientManager([with_header], environment=ENV, oauth=manager)
    # The secret reference is checked first, before the optional package is imported.
    with pytest.raises(ConfigurationError, match="MISSING_WORKSPACE_HEADER"):
        await mcp.connect("slack", tenant_id="tenant-a")
    mcp = MCPClientManager([server_config()], environment=ENV, oauth=manager)
    with pytest.raises(PolicyDeniedError, match=r"algen-agent-runtime\[mcp\]"):
        await mcp.connect("slack", tenant_id="tenant-a")


def test_missing_encryption_extra_is_actionable_after_the_key_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algen_agent_runtime.security.encryption import AesGcmCipher, generate_key

    monkeypatch.setitem(sys.modules, "cryptography", None)
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.ciphers.aead", None)
    with pytest.raises(ConfigurationError, match="MCP_TOKEN_KEY"):
        AesGcmCipher.from_references("env://MCP_TOKEN_KEY", environment={})
    with pytest.raises(ConfigurationError, match="32-byte"):
        AesGcmCipher.from_references(
            "env://MCP_TOKEN_KEY", environment={"MCP_TOKEN_KEY": "c2hvcnQ"}
        )
    with pytest.raises(ImportError, match=r"algen-agent-runtime\[encryption\]"):
        AesGcmCipher.from_references(
            "env://MCP_TOKEN_KEY", environment={"MCP_TOKEN_KEY": generate_key()}
        )


# -- encrypted PostgreSQL store -----------------------------------------------------------------


class FakeDatabase:
    """Just enough PostgreSQL for PostgresMCPTokenStore's statements."""

    def __init__(self) -> None:
        self.tokens: dict[tuple[str, str, str], str] = {}
        self.states: dict[str, tuple[str, datetime]] = {}

    async def execute(self, query: str, *args: Any) -> str:
        if query.startswith("INSERT INTO algen_agent_runtime_mcp_oauth_tokens"):
            self.tokens[(args[0], args[1], args[2])] = args[3]
        elif query.startswith("DELETE FROM algen_agent_runtime_mcp_oauth_states WHERE expires_at"):
            self.states = {k: v for k, v in self.states.items() if v[1] > args[0]}
        elif query.startswith("INSERT INTO algen_agent_runtime_mcp_oauth_states"):
            self.states[args[0]] = (args[3], args[4])
        else:
            raise AssertionError(query)
        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if query.startswith("SELECT ciphertext FROM algen_agent_runtime_mcp_oauth_tokens"):
            value = self.tokens.get((args[0], args[1], args[2]))
            return {"ciphertext": value} if value else None
        if query.startswith("DELETE FROM algen_agent_runtime_mcp_oauth_tokens"):
            removed = self.tokens.pop((args[0], args[1], args[2]), None)
            return {"server": args[2]} if removed else None
        if query.startswith("DELETE FROM algen_agent_runtime_mcp_oauth_states WHERE state_hash"):
            state = self.states.pop(args[0], None)
            return {"ciphertext": state[0]} if state else None
        raise AssertionError(query)


def encrypted_store(database: FakeDatabase) -> PostgresMCPTokenStore:
    pytest.importorskip("cryptography")
    from algen_agent_runtime.security.encryption import AesGcmCipher, generate_key

    key = generate_key()
    return PostgresMCPTokenStore(
        database, AesGcmCipher.from_references("env://KEY", environment={"KEY": key})
    )


async def test_postgres_store_encrypts_and_binds_rows_to_their_tenant() -> None:
    from algen_agent_runtime.security.encryption import DecryptionError

    database = FakeDatabase()
    store = encrypted_store(database)
    manager, provider = make_manager(store=store)
    started = await manager.start("slack", "tenant-a", "alice")
    stored_state = next(iter(database.states.values()))[0]
    assert "alice" not in stored_state and started.authorize_url not in stored_state
    code, state = provider.consent(started.authorize_url)
    await manager.complete(state, code)
    assert database.states == {}
    ciphertext = database.tokens[("tenant-a", "", "slack")]
    assert ciphertext.startswith("v1.")
    assert "access-secret" not in ciphertext and "refresh-secret" not in ciphertext
    assert await manager.access_token("slack", "tenant-a", "alice") == "access-secret-1"
    # A row copied into another tenant's key does not decrypt: the key is the associated data.
    database.tokens[("tenant-b", "", "slack")] = ciphertext
    with pytest.raises(DecryptionError):
        await store.get(MCPTokenKey("tenant-b", None, "slack"))
    # Revoke still removes a row it cannot read.
    assert (await manager.revoke("slack", "tenant-b", "bob")).removed
    with pytest.raises(MCPOAuthStateError):
        await manager.complete(state, code)


def test_container_selects_the_encrypted_store_without_importing_the_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("cryptography")
    from algen_agent_runtime.security.encryption import generate_key

    monkeypatch.setitem(sys.modules, "asyncpg", None)
    settings = AppSettings.model_validate(
        {
            "storage": {
                "postgres_dsn": "env://DSN",
                "mcp_oauth_token_store": "postgres",
                "mcp_oauth_encryption_key": "env://MCP_TOKEN_KEY",
            },
            "mcp_servers": [server_config().model_dump(mode="json")],
        }
    )
    container = build_container(
        settings,
        environment={"DSN": "postgresql://localhost/test", "MCP_TOKEN_KEY": generate_key(), **ENV},
    )
    assert container.mcp is not None and container.mcp.oauth is not None
    assert isinstance(container.mcp.oauth.store, PostgresMCPTokenStore)
    assert build_mcp_oauth_manager([], settings.storage, database=None) is None
    container.close()


# -- HTTP routes --------------------------------------------------------------------------------


def headers(
    tenant: str = "tenant-a", user: str = "alice", scopes: str = "mcp:connect mcp:admin"
) -> dict[str, str]:
    return {"x-tenant-id": tenant, "x-user-id": user, "x-scopes": scopes}


async def test_routes_run_the_flow_and_never_return_a_token() -> None:
    settings = AppSettings.model_validate(
        {"mcp_servers": [server_config().model_dump(mode="json")]}
    )
    container = build_container(settings, environment=ENV)
    manager, provider = make_manager()
    mcp, _ = client_manager(manager)
    container = dataclasses.replace(container, mcp=mcp)
    app = create_app(settings, container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://runtime.example.com"
    ) as client:
        denied = await client.post(
            "/v1/mcp/servers/slack/oauth/start", headers=headers(scopes="mcp:connect")
        )
        assert denied.status_code == 403  # a tenant-wide connection needs mcp:admin
        started = await client.post("/v1/mcp/servers/slack/oauth/start", headers=headers())
        assert started.status_code == 200
        code, state = provider.consent(started.json()["authorize_url"])

        callback = await client.get("/v1/mcp/oauth/callback", params={"state": state, "code": code})
        assert callback.status_code == 200
        assert callback.headers["cache-control"] == "no-store"
        assert callback.json()["connected"] is True and callback.json()["tools_ready"] is True
        assert "mcp.slack.post_message" in {tool.definition.name for tool in container.tools.list()}
        replay = await client.get("/v1/mcp/oauth/callback", params={"state": state, "code": code})
        assert replay.status_code == 400
        assert replay.json()["code"] == "mcp_oauth_invalid_state"

        status = await client.get(
            "/v1/mcp/servers/slack/oauth/status", headers=headers(scopes="mcp:read")
        )
        other = await client.get(
            "/v1/mcp/servers/slack/oauth/status", headers=headers("tenant-b", scopes="mcp:read")
        )
        listing = await client.get("/v1/mcp/oauth/servers", headers=headers(scopes="mcp:read"))
        assert status.json()["connected"] is True and other.json()["connected"] is False
        assert listing.json()["servers"][0]["server"] == "slack"
        unknown = await client.get("/v1/mcp/servers/nope/oauth/status", headers=headers())
        assert unknown.status_code == 404

        revoked = await client.delete("/v1/mcp/servers/slack/oauth", headers=headers())
        assert revoked.json() == {"server": "slack", "removed": True, "provider_revoked": True}
        after = await client.get("/v1/mcp/servers/slack/oauth/status", headers=headers())
        assert after.json()["connected"] is False

        bodies = " ".join(
            response.text for response in (started, callback, status, listing, revoked, after)
        )
    audits = await container.runtime.audits.list("tenant-a")
    assert {audit.action for audit in audits} >= {"mcp.oauth.connect", "mcp.oauth.revoke"}
    recorded = bodies + " ".join(audit.model_dump_json() for audit in audits)
    assert "access-secret" not in recorded and "refresh-secret" not in recorded
    assert ENV["SLACK_CLIENT_SECRET"] not in recorded


async def test_callback_redirects_to_the_configured_page() -> None:
    target = "https://studio.example.com/connections?tab=mcp"
    settings = AppSettings.model_validate(
        {"mcp_servers": [server_config().model_dump(mode="json")]}
    )
    container = build_container(settings, environment=ENV)
    manager, provider = make_manager(success_redirect_url=target)
    container = dataclasses.replace(container, mcp=client_manager(manager)[0])
    app = create_app(settings, container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://runtime.example.com"
    ) as client:
        started = await client.post("/v1/mcp/servers/slack/oauth/start", headers=headers())
        code, state = provider.consent(started.json()["authorize_url"])
        ok = await client.get("/v1/mcp/oauth/callback", params={"state": state, "code": code})
        assert ok.status_code == 303
        assert ok.headers["location"] == f"{target}&mcp_server=slack&status=connected"
        started = await client.post("/v1/mcp/servers/slack/oauth/start", headers=headers())
        _, state = provider.consent(started.json()["authorize_url"])
        refused = await client.get(
            "/v1/mcp/oauth/callback", params={"state": state, "error": "access_denied"}
        )
        assert refused.status_code == 303
        assert "status=error" in refused.headers["location"]
