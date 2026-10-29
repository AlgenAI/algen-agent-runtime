"""Declarative HTTP API tools, with a scripted transport in place of the network."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

import algen_agent_runtime.tools.http_api as http_api
from algen_agent_runtime.cache import CachePolicy, CacheScope, CacheService, InMemoryCacheStore
from algen_agent_runtime.config.settings import AppSettings, SecuritySettings
from algen_agent_runtime.exceptions.errors import ToolExecutionError
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    ToolContext,
    ToolExecutionStatus,
)
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.http_api import HttpToolConfig, http_api_tool
from algen_agent_runtime.tools.registry import ToolRegistry

CONTEXT = ToolContext(
    run_id="r",
    step_id="s",
    tenant_id="t",
    user_id="u",
    permissions=frozenset(),
    idempotency_key="k",
)
HOSTS = ("api.example.com",)


@pytest.fixture(autouse=True)
def no_dns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record outbound URL checks instead of resolving real hosts."""
    checked: list[str] = []

    async def check(url: str, allowed: tuple[str, ...], private: bool) -> list[str]:
        checked.append(url)
        return ["93.184.216.34"]

    monkeypatch.setattr(http_api, "validate_outbound_url", check)
    return checked


class Server:
    """Answers each request from a script and records what was asked."""

    def __init__(self, *replies: httpx.Response) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.replies.pop(0)


def _tool(config: dict[str, Any], server: Server, secrets: dict[str, str] | None = None) -> Any:
    async def resolve(reference: str) -> str:
        return (secrets or {})[reference.removeprefix("env://")]

    return http_api_tool(
        HttpToolConfig.model_validate(config),
        allowed_hosts=HOSTS,
        secrets=resolve,
        client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
    )


def _json(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers or {})


ISSUE = {
    "name": "github.issue",
    "description": "Read one issue",
    "url": "https://api.example.com/repos/{owner}/{repo}/issues/{number}",
    "parameters": {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "number": {"type": "integer"},
            "fields": {"type": "string"},
        },
        "required": ["owner", "repo", "number"],
    },
    "query": {"fields": "{fields}", "v": "3"},
    "auth": {"type": "bearer", "token": "env://GITHUB_TOKEN"},
    "response": {"select": "/issue"},
}


async def test_a_call_fills_the_url_query_and_auth_and_selects_the_answer(
    no_dns: list[str],
) -> None:
    server = Server(_json({"issue": {"title": "Broken"}, "noise": 1}))
    tool = _tool(ISSUE, server, {"GITHUB_TOKEN": "ghs_secret"})
    result = await tool.handler({"owner": "algen ai", "repo": "studio", "number": 7}, CONTEXT)
    assert result == {
        "ok": True,
        "status": 200,
        "headers": {"content-type": "application/json"},
        "data": {"title": "Broken"},
        "pages": 1,
    }
    [request] = server.requests
    # Path values are escaped; a query entry whose argument is missing is left out.
    assert str(request.url) == "https://api.example.com/repos/algen%20ai/studio/issues/7?v=3"
    assert request.headers["authorization"] == "Bearer ghs_secret"
    assert no_dns == ["https://api.example.com/repos/algen%20ai/studio/issues/7"]
    assert tool.definition.side_effect == SideEffect.READ
    assert tool.definition.idempotency == Idempotency.IDEMPOTENT


@pytest.mark.parametrize("value", ["..", ".", "%2e%2e", "%2E", ".%2e", ""])
async def test_a_path_value_cannot_be_a_dot_segment(value: str) -> None:
    server = Server(_json({}))
    tool = _tool(ISSUE, server, {"GITHUB_TOKEN": "ghs_secret"})

    with pytest.raises(ToolExecutionError, match="must name one path segment"):
        await tool.handler({"owner": "algen", "repo": value, "number": 7}, CONTEXT)

    assert server.requests == []


async def test_a_path_value_with_dots_is_still_one_segment() -> None:
    server = Server(_json({"issue": {}}), _json({"issue": {}}))
    tool = _tool(ISSUE, server, {"GITHUB_TOKEN": "ghs_secret"})

    await tool.handler({"owner": "algen", "repo": "...", "number": 7}, CONTEXT)
    await tool.handler({"owner": "algen", "repo": "../secrets", "number": 7}, CONTEXT)

    assert [r.url.raw_path for r in server.requests] == [
        b"/repos/algen/.../issues/7?v=3",
        b"/repos/algen/..%2Fsecrets/issues/7?v=3",
    ]


async def test_the_urls_own_query_is_kept_with_templates_and_pages() -> None:
    server = Server(_json({"items": [1]}), _json({"items": []}))
    search = {
        "name": "api.search",
        "description": "Search",
        "url": "https://api.example.com/search?q={term}&v=2&page=9",
        "parameters": {
            "type": "object",
            "properties": {"term": {"type": "string"}},
            "required": ["term"],
        },
        "query": {"sort": "new"},
        "pagination": {"type": "page_param", "items_pointer": "/items"},
    }

    result = await _tool(search, server).handler({"term": "a b"}, CONTEXT)

    assert result["data"] == [1]
    assert [str(r.url) for r in server.requests] == [
        "https://api.example.com/search?q=a+b&v=2&sort=new&page=1",
        "https://api.example.com/search?q=a+b&v=2&sort=new&page=2",
    ]


async def test_a_body_keeps_argument_types_and_basic_and_header_auth_work() -> None:
    server = Server(_json({"id": 1}, status=201), _json({"id": 2}))
    create = _tool(
        {
            "name": "tickets.create",
            "description": "Open a ticket",
            "method": "POST",
            "url": "https://api.example.com/tickets",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "priority": {"type": "integer"},
                    "labels": {"type": "array"},
                },
                "required": ["title"],
            },
            "body": {
                "title": "{title}",
                "priority": "{priority}",
                "labels": "{labels}",
                "summary": "New: {title}",
            },
            "auth": {"type": "basic", "username": "env://USER", "password": "env://PASS"},
        },
        server,
        {"USER": "bot", "PASS": "pw"},
    )
    result = await create.handler({"title": "Down", "priority": 2, "labels": ["ops"]}, CONTEXT)
    assert result["ok"] and result["status"] == 201
    request = server.requests[0]
    assert json.loads(request.content) == {
        "title": "Down",
        "priority": 2,
        "labels": ["ops"],
        "summary": "New: Down",
    }
    assert request.headers["authorization"] == "Basic " + base64.b64encode(b"bot:pw").decode()
    assert create.definition.side_effect == SideEffect.EXTERNAL, "a write asks for approval"

    keyed = _tool(
        {
            "name": "status.read",
            "description": "s",
            "url": "https://api.example.com/status",
            "auth": {"type": "header", "header_name": "X-Api-Key", "token": "env://KEY"},
        },
        server,
        {"KEY": "k-123"},
    )
    await keyed.handler({}, CONTEXT)
    assert server.requests[1].headers["x-api-key"] == "k-123"


async def test_an_error_status_is_reported_not_raised() -> None:
    server = Server(_json({"message": "Not Found"}, status=404))
    result = await _tool(ISSUE, server, {"GITHUB_TOKEN": "t"}).handler(
        {"owner": "a", "repo": "b", "number": 1}, CONTEXT
    )
    assert result["ok"] is False and result["status"] == 404
    assert result["data"] == {"message": "Not Found"}


async def test_link_header_pagination_follows_next_up_to_the_page_limit(
    no_dns: list[str],
) -> None:
    next_link = '<https://api.example.com/items?cursor=b>; rel="next"'
    server = Server(
        _json({"items": [1, 2]}, headers={"link": next_link}),
        _json(
            {"items": [3]}, headers={"link": '<https://api.example.com/items?cursor=c>; rel="next"'}
        ),
    )
    tool = _tool(
        {
            "name": "items.list",
            "description": "i",
            "url": "https://api.example.com/items",
            "query": {"per_page": "2"},
            "pagination": {"type": "link_header", "items_pointer": "/items", "max_pages": 2},
        },
        server,
    )
    result = await tool.handler({}, CONTEXT)
    assert result["data"] == [1, 2, 3] and result["pages"] == 2
    assert [str(r.url) for r in server.requests] == [
        "https://api.example.com/items?per_page=2",
        "https://api.example.com/items?cursor=b",
    ]
    assert len(no_dns) == 2, "every page's URL is checked against the allowlist"


async def test_page_param_pagination_stops_at_an_empty_page() -> None:
    server = Server(_json([1, 2]), _json([3]), _json([]))
    tool = _tool(
        {
            "name": "rows.list",
            "description": "r",
            "url": "https://api.example.com/rows",
            "pagination": {"type": "page_param", "page_param": "p", "max_pages": 10},
        },
        server,
    )
    result = await tool.handler({}, CONTEXT)
    assert result["data"] == [1, 2, 3] and result["pages"] == 3
    assert [r.url.params["p"] for r in server.requests] == ["1", "2", "3"]


async def test_a_response_over_the_limit_is_refused() -> None:
    server = Server(_json({"blob": "x" * 5000}))
    tool = _tool(
        {
            "name": "big.read",
            "description": "b",
            "url": "https://api.example.com/big",
            "max_result_bytes": 1024,
        },
        server,
    )
    with pytest.raises(ToolExecutionError, match="larger than 1024 bytes"):
        await tool.handler({}, CONTEXT)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"url": "https://{host}.example.com/x"}, "cannot template the host"),
        ({"url": "ftp://api.example.com/x"}, "absolute http"),
        ({"query": {"q": "{missing}"}}, "not a parameter"),
        ({"url": "https://api.example.com/{id}"}, "must be a required parameter"),
        ({"body": {"a": 1}}, "cannot send a body with GET"),
        ({"headers": {"Authorization": "x"}}, "use auth for credentials"),
        ({"auth": {"type": "bearer", "token": "plain-secret"}}, "env:// secret reference"),
        ({"auth": {"type": "basic", "username": "env://U"}}, "requires password"),
        ({"response": {"select": "data"}}, "JSON pointer"),
    ],
)
def test_declarations_are_checked(change: dict[str, Any], message: str) -> None:
    base = {"name": "x.read", "description": "d", "url": "https://api.example.com/x"}
    with pytest.raises(ValidationError, match=message):
        HttpToolConfig.model_validate({**base, **change})


@pytest.mark.parametrize(
    ("tools", "hosts", "message"),
    [
        (
            [{"name": "a.read", "url": "https://evil.example.org/x"}],
            ("api.example.com",),
            "must be listed in security.allowed_http_hosts",
        ),
        (
            [{"name": "a.read", "url": "https://api.example.com/x"}],
            (),
            "must be listed in security.allowed_http_hosts",
        ),
        (
            [{"name": "a.read", "url": "https://api.example.com/x"}] * 2,
            ("api.example.com",),
            "must be unique",
        ),
        (
            [{"name": "core.http", "url": "https://api.example.com/x"}],
            ("api.example.com",),
            "core. prefix",
        ),
    ],
)
def test_settings_require_listed_hosts_and_unique_names(
    tools: list[dict[str, Any]], hosts: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        AppSettings(
            http_tools=tuple({"description": "d", **tool} for tool in tools),  # type: ignore[arg-type]
            security=SecuritySettings(allowed_http_hosts=hosts),
        )


def test_declared_tools_are_registered_in_the_container() -> None:
    settings = AppSettings(
        http_tools=(
            HttpToolConfig(
                name="status.read", description="d", url="https://status.example.com/now"
            ),
        ),
        security=SecuritySettings(allowed_http_hosts=("example.com",)),
    )
    container = build_container(settings)
    tool = container.runtime.tools.get("status.read")
    assert tool.definition.description == "d"


CALLER = {
    "name": "lms.me",
    "description": "Read the signed-in user's profile",
    "url": "https://api.example.com/me",
    "auth": {"type": "caller"},
}
USER_TOKEN = "Bearer user-token-3f9a1c"


def _caller_context(**credentials: str) -> ToolContext:
    return CONTEXT.model_copy(update={"credentials": credentials})


async def test_caller_auth_forwards_the_runs_credential_as_given() -> None:
    server = Server(_json({"id": "u"}))
    tool = _tool(CALLER, server)

    result = await tool.handler({}, _caller_context(authorization=USER_TOKEN))

    assert result["ok"] is True
    [request] = server.requests
    assert request.headers["authorization"] == USER_TOKEN
    assert "cookie" not in request.headers
    assert USER_TOKEN not in json.dumps(result)


@pytest.mark.parametrize("given", ["user-token-3f9a1c", "bearer user-token-3f9a1c"])
async def test_caller_auth_applies_a_scheme_and_a_custom_header(given: str) -> None:
    server = Server(_json({}), _json({}))
    bearer = _tool({**CALLER, "auth": {"type": "caller", "scheme": "Bearer"}}, server)
    custom = _tool(
        {**CALLER, "auth": {"type": "caller", "credential": "lms_key", "header_name": "X-Key"}},
        server,
    )

    await bearer.handler({}, _caller_context(authorization=given))
    await custom.handler({}, _caller_context(lms_key="key-1", authorization=USER_TOKEN))

    assert server.requests[0].headers["authorization"] == "Bearer user-token-3f9a1c"
    assert server.requests[1].headers["x-key"] == "key-1"
    assert "authorization" not in server.requests[1].headers


async def test_caller_auth_without_the_credential_sends_nothing() -> None:
    server = Server(_json({}))
    plain = _tool(CALLER, server)
    bearer = _tool({**CALLER, "auth": {"type": "caller", "scheme": "Bearer"}}, server)
    contexts = [
        CONTEXT,
        _caller_context(other="x"),
        _caller_context(authorization=""),
        _caller_context(authorization="   "),
        _caller_context(authorization="\t"),
    ]

    for tool in (plain, bearer):
        for context in contexts:
            with pytest.raises(ToolExecutionError, match="authentication required"):
                await tool.handler({}, context)
    # With a scheme, the scheme alone is no credential either.
    for given in ("Bearer ", "bearer", "  Bearer\t "):
        with pytest.raises(ToolExecutionError, match="authentication required"):
            await bearer.handler({}, _caller_context(authorization=given))

    assert server.requests == []


async def test_caller_auth_refuses_a_credential_of_another_scheme() -> None:
    server = Server(_json({}))
    tool = _tool({**CALLER, "auth": {"type": "caller", "scheme": "Bearer"}}, server)

    for given in ("Basic dXNlcjpwdw==", "Bearer Bearer tok", "tok en"):
        with pytest.raises(ToolExecutionError, match="not a Bearer credential") as raised:
            await tool.handler({}, _caller_context(authorization=given))
        assert "dXNl" not in str(raised.value) and "tok" not in str(raised.value)

    assert server.requests == []


async def test_caller_auth_refuses_a_credential_that_is_not_a_header_value() -> None:
    server = Server(_json({}))
    tool = _tool(CALLER, server)
    # A trailing line break is refused too, not silently dropped.
    for injected in ("Bearer abc\r\nX-Injected: 1", "Bearer abc\r\n", "\nBearer abc"):
        with pytest.raises(ToolExecutionError, match="not a valid header value") as raised:
            await tool.handler({}, _caller_context(authorization=injected))
        assert "abc" not in str(raised.value)

    assert server.requests == []


async def test_caller_auth_never_follows_a_link_to_another_origin() -> None:
    paged = {
        **CALLER,
        "name": "lms.items",
        "url": "https://api.example.com/items",
        "pagination": {"type": "link_header", "items_pointer": "/items"},
    }
    same = Server(
        _json({"items": [1]}, headers={"link": '<https://api.example.com/items?c=2>; rel="next"'}),
        _json({"items": [2]}),
    )
    other = Server(
        _json(
            {"items": [1]},
            headers={"link": '<https://files.api.example.com/items?c=2>; rel="next"'},
        ),
        _json({"items": [2]}),
    )

    result = await _tool(paged, same).handler({}, _caller_context(authorization=USER_TOKEN))
    assert result["data"] == [1, 2]
    assert [r.headers["authorization"] for r in same.requests] == [USER_TOKEN] * 2
    with pytest.raises(ToolExecutionError, match="another origin"):
        await _tool(paged, other).handler({}, _caller_context(authorization=USER_TOKEN))
    assert len(other.requests) == 1


@pytest.mark.parametrize(
    ("auth", "message"),
    [
        ({"type": "caller", "token": "env://T"}, "takes no token"),
        ({"type": "caller", "header_name": "Cookie"}, "cannot be sent as"),
        ({"type": "caller", "header_name": "Proxy-Authorization"}, "cannot be sent as"),
        ({"type": "caller", "header_name": "TE"}, "cannot be sent as"),
        ({"type": "caller", "header_name": "Upgrade"}, "cannot be sent as"),
        ({"type": "caller", "header_name": "Keep-Alive"}, "cannot be sent as"),
        ({"type": "caller", "header_name": "Trailer"}, "cannot be sent as"),
        ({"type": "caller", "header_name": "Proxy-Connection"}, "cannot be sent as"),
        ({"type": "caller", "credential": "bad key"}, "credential"),
        ({"type": "caller", "scheme": "Bearer token"}, "scheme"),
        ({"type": "bearer", "token": "env://T", "scheme": "Bearer"}, "caller authentication only"),
    ],
)
def test_caller_auth_declarations_are_checked(auth: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        HttpToolConfig.model_validate({**CALLER, "auth": auth})


@pytest.mark.parametrize(
    "auth",
    [
        {"type": "caller", "header_name": "X-User-Token"},
        {"type": "header", "header_name": "X-User-Token", "token": "env://T"},
    ],
)
def test_a_header_template_cannot_write_into_the_auth_header(auth: dict[str, Any]) -> None:
    config = {
        **CALLER,
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
        "auth": auth,
        "headers": {"x-user-token": "{id}"},
    }
    with pytest.raises(ValidationError, match="use auth for credentials"):
        HttpToolConfig.model_validate(config)


async def test_a_write_refused_before_any_request_is_a_definite_failure() -> None:
    server = Server(_json({"enrolled": True}))
    enrol = {
        **CALLER,
        "name": "lms.enrol",
        "method": "POST",
        "url": "https://api.example.com/enrolments",
        "body": {"course": "c1"},
    }
    registry = ToolRegistry()
    registry.register(_tool(enrol, server))
    executor = ToolExecutor(registry, CompositePolicyEngine())

    with pytest.raises(ToolExecutionError, match="authentication required"):
        await executor.execute("lms.enrol", {}, CONTEXT)

    record = await executor.executions.get("k", "t")  # type: ignore[attr-defined]
    # Nothing was sent, so there is nothing to reconcile: the same call can run once signed in.
    assert record.status == ToolExecutionStatus.FAILED
    result = await executor.execute("lms.enrol", {}, _caller_context(authorization=USER_TOKEN))
    assert result.value["data"] == {"enrolled": True}
    assert len(server.requests) == 1


async def test_calls_with_caller_credentials_bypass_the_tool_result_cache() -> None:
    server = Server(*(_json({"profile": index}) for index in range(3)))
    registry = ToolRegistry()
    registry.register(_tool(CALLER, server))
    cache = CacheService(
        InMemoryCacheStore(), {"tool_results": CachePolicy(enabled=True, scope=CacheScope.TENANT)}
    )
    executor = ToolExecutor(registry, CompositePolicyEngine(), cache=cache)

    def context(user: str, key: str) -> ToolContext:
        return CONTEXT.model_copy(
            update={
                "user_id": user,
                "idempotency_key": key,
                "credentials": {"authorization": f"Bearer {user}"},
            }
        )

    first = await executor.execute("lms.me", {}, context("alice", "k1"))
    second = await executor.execute("lms.me", {}, context("bob", "k2"))

    # Same tool, arguments and tenant, but each call carries a different caller's credential.
    assert first.value["data"] == {"profile": 0}
    assert second.value["data"] == {"profile": 1}
    assert not second.metadata.get("runtime_cache_hit")
    assert [r.headers["authorization"] for r in server.requests] == ["Bearer alice", "Bearer bob"]
    assert len(cache.store._items) == 0  # type: ignore[attr-defined]

    # The same read without credentials is cached as before.
    public = Server(_json({"status": "up"}))
    registry.register(_tool({**CALLER, "name": "lms.status", "auth": {"type": "none"}}, public))
    plain = [CONTEXT.model_copy(update={"idempotency_key": key}) for key in ("k3", "k4")]
    await executor.execute("lms.status", {}, plain[0])
    cached = await executor.execute("lms.status", {}, plain[1])
    assert cached.metadata.get("runtime_cache_hit") is True
    assert len(public.requests) == 1
