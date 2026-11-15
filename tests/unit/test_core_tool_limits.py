"""core.http needs an explicit host policy and caps its body; transition IPv6 addresses are
checked by the IPv4 address they carry."""

from __future__ import annotations

import ipaddress
from typing import Any

import httpx
import pytest

import algen_agent_runtime.tools.builtin as builtin
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.exceptions.errors import PolicyDeniedError
from algen_agent_runtime.security.network import is_ip_allowed
from algen_agent_runtime.tools.builtin import MAX_HTTP_BODY_BYTES, http_tool
from algen_agent_runtime.tools.contracts import ToolContext

CONTEXT = ToolContext(
    run_id="r",
    step_id="s",
    tenant_id="t",
    user_id="u",
    permissions=frozenset({"network.http"}),
    idempotency_key="k",
)


@pytest.fixture
def no_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check(url: str, allowed: tuple[str, ...], private: bool) -> list[str]:
        from algen_agent_runtime.security.network import check_host_allowlist

        check_host_allowlist(httpx.URL(url).host, allowed)
        return ["93.184.216.34"]

    monkeypatch.setattr(builtin, "validate_outbound_url", check)


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_core_http_without_allowed_hosts_refuses_every_call(no_dns: None) -> None:
    calls: list[httpx.Request] = []
    tool = http_tool(
        allowed_hosts=(), client=_client(lambda r: calls.append(r) or httpx.Response(200))
    )
    with pytest.raises(PolicyDeniedError, match="allow_any_public_host"):
        await tool.execute({"url": "https://any.example.com/"}, CONTEXT)
    assert calls == []


async def test_core_http_reaches_any_public_host_only_when_allowed(no_dns: None) -> None:
    tool = http_tool(
        allowed_hosts=(),
        allow_any_public_host=True,
        client=_client(lambda r: httpx.Response(200, text="ok")),
    )
    result = await tool.execute({"url": "https://any.example.com/"}, CONTEXT)
    assert result["status"] == 200 and result["body"] == "ok" and result["truncated"] is False


def test_allow_any_public_host_is_off_by_default() -> None:
    settings = AppSettings.model_validate({"security": {"allow_any_public_host": True}})
    assert settings.security.allow_any_public_host is True
    assert AppSettings().security.allow_any_public_host is False


async def test_core_http_streams_and_truncates_a_large_body(no_dns: None) -> None:
    produced: list[int] = []

    async def body() -> Any:
        for _ in range(10_000):
            produced.append(1)
            yield b"y" * 4096

    tool = http_tool(
        allowed_hosts=("api.example.com",),
        client=_client(lambda r: httpx.Response(200, content=body())),
    )
    result = await tool.execute({"url": "https://api.example.com/big"}, CONTEXT)
    assert result["truncated"] is True
    assert len(result["body"]) == MAX_HTTP_BODY_BYTES
    assert len(produced) < 100  # stopped reading long before the 40 MB body ended


@pytest.mark.parametrize(
    "address",
    [
        "2002:7f00:1::",  # 6to4 for 127.0.0.1
        "2002:a9fe:a9fe::",  # 6to4 for the metadata address
        "2002:c0a8:101::1",  # 6to4 for 192.168.1.1
        "2001:0:4136:e378:8000:63bf:80ff:fffe",  # Teredo, client 127.0.0.1
        "64:ff9b::7f00:1",  # NAT64 for 127.0.0.1
        "64:ff9b::a9fe:a9fe",  # NAT64 for the metadata address
    ],
)
def test_transition_addresses_carrying_private_ipv4_are_refused(address: str) -> None:
    assert is_ip_allowed(ipaddress.ip_address(address)) is False


def test_transition_addresses_are_refused_even_for_a_public_ipv4() -> None:
    # The same on every Python release (3.12.0 calls 2002::/16 global, 3.12.16 private).
    for address in ("2002:808:808::1", "2001:0:4136:e378:8000:63bf:f7f7:f7f7", "64:ff9b::808:808"):
        assert is_ip_allowed(ipaddress.ip_address(address)) is False, address
