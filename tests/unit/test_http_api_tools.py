"""Declarative HTTP API tools, with a scripted transport in place of the network."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

import algen_agent_runtime.tools.http_api as http_api
from algen_agent_runtime.config.settings import AppSettings, SecuritySettings
from algen_agent_runtime.exceptions.errors import ToolExecutionError
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.tools.contracts import Idempotency, SideEffect, ToolContext
from algen_agent_runtime.tools.http_api import HttpToolConfig, http_api_tool

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
