from __future__ import annotations

import ipaddress
import socket
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from algen_agent_runtime.exceptions.errors import PolicyDeniedError
from algen_agent_runtime.model_services.contracts import (
    ModelServiceKind,
    ModelServiceManifest,
    ModelServiceRequest,
)
from algen_agent_runtime.model_services.http import HTTPModelService
from algen_agent_runtime.security.network import (
    SafeNetworkBackend,
    check_host_allowlist,
    create_safe_http_client,
    is_ip_allowed,
    normalize_and_validate_url,
    resolve_and_validate_addresses,
    validate_outbound_url,
)
from algen_agent_runtime.tools.adapters import remote_tool
from algen_agent_runtime.tools.builtin import http_tool
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    ToolContext,
    ToolDefinition,
)


class TestIPFiltering:
    """Test comprehensive IP address filtering against SSRF and cloud metadata access."""

    @pytest.mark.parametrize(
        "ip_str",
        [
            # Loopback
            "127.0.0.1",
            "127.0.0.2",
            "127.1.2.3",
            "::1",
            # RFC 1918 Private networks
            "10.0.0.1",
            "10.254.0.1",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.0.1",
            "192.168.1.100",
            # Carrier-grade NAT (RFC 6598)
            "100.64.0.1",
            "100.127.255.255",
            # Link-Local (RFC 3927)
            "169.254.1.1",
            "fe80::1",
            # Cloud Metadata endpoints
            "169.254.169.254",  # AWS / GCP / Azure IMDS
            "169.254.170.2",  # AWS ECS task metadata
            "fd00:ec2::254",  # AWS IMDSv6
            "100.100.100.200",  # Alibaba Cloud metadata
            # Unspecified and Broadcast / Multicast
            "0.0.0.0",
            "::",
            "224.0.0.1",
            "239.255.255.255",
            "ff02::1",
            # IPv4-mapped IPv6 pointing to private/loopback/metadata
            "::ffff:127.0.0.1",
            "::ffff:10.0.0.1",
            "::ffff:169.254.169.254",
            "::ffff:192.168.1.1",
        ],
    )
    def test_forbidden_ips_rejected_by_default(self, ip_str: str) -> None:
        ip = ipaddress.ip_address(ip_str)
        assert is_ip_allowed(ip, allow_private_networks=False) is False

    @pytest.mark.parametrize(
        "ip_str",
        [
            "8.8.8.8",
            "1.1.1.1",
            "93.184.216.34",
            "2607:f8b0:4005:805::200e",
            "::ffff:8.8.8.8",
        ],
    )
    def test_public_routable_ips_allowed(self, ip_str: str) -> None:
        ip = ipaddress.ip_address(ip_str)
        assert is_ip_allowed(ip, allow_private_networks=False) is True

    def test_allow_private_networks_override(self) -> None:
        assert is_ip_allowed(ipaddress.ip_address("127.0.0.1"), allow_private_networks=True) is True
        assert is_ip_allowed(ipaddress.ip_address("10.0.0.1"), allow_private_networks=True) is True
        assert (
            is_ip_allowed(ipaddress.ip_address("169.254.169.254"), allow_private_networks=True)
            is True
        )


class TestURLNormalizationAndHostAllowlist:
    """Test URL scheme, host allowlist, and userinfo validation."""

    def test_valid_http_and_https_schemes(self) -> None:
        host, port = normalize_and_validate_url("http://example.com/api")
        assert host == "example.com"
        assert port == 80

        host, port = normalize_and_validate_url("https://example.com:8443/api")
        assert host == "example.com"
        assert port == 8443

    @pytest.mark.parametrize(
        "bad_url",
        [
            "ftp://example.com/resource",
            "file:///etc/passwd",
            "gopher://example.com",
            "data:text/plain;base64,SGVsbG8=",
            "javascript:alert(1)",
        ],
    )
    def test_disallowed_schemes_rejected(self, bad_url: str) -> None:
        with pytest.raises(PolicyDeniedError, match="is not allowed"):
            normalize_and_validate_url(bad_url)

    def test_embedded_credentials_rejected(self) -> None:
        with pytest.raises(PolicyDeniedError, match="embedded user credentials"):
            normalize_and_validate_url("http://user:secret@example.com/api")

    def test_missing_hostname_rejected(self) -> None:
        with pytest.raises(PolicyDeniedError, match="must use http or https and include a host"):
            normalize_and_validate_url("http:///path")

    def test_percent_encoded_host_decoded_and_checked(self) -> None:
        # %31%32%37%2E%30%2E%30%2E%31 is 127.0.0.1
        host, port = normalize_and_validate_url("http://%31%32%37%2E%30%2E%30%2E%31:8080/test")
        assert host == "127.0.0.1"
        assert port == 8080

    def test_host_allowlist_matching(self) -> None:
        # Exact match
        check_host_allowlist("api.example.com", ("api.example.com", "other.com"))

        # Wildcard / domain suffix match
        check_host_allowlist("sub.service.example.com", ("example.com",))
        check_host_allowlist("sub.service.example.com", (".example.com",))

        # Disallowed host
        with pytest.raises(PolicyDeniedError, match="is not allowlisted"):
            check_host_allowlist("evil.com", ("example.com",))


class TestDNSResolutionAndRebindingProtection:
    """Test single DNS resolution and fail-closed rebinding mitigation."""

    def test_all_resolved_ips_validated(self) -> None:
        def mock_resolver(host: str, port: int) -> list[tuple[Any, ...]]:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            ]

        validated = resolve_and_validate_addresses("example.com", 80, resolver=mock_resolver)
        assert validated == ["93.184.216.34"]

    def test_mixed_public_and_private_ips_fails_closed(self) -> None:
        # If any returned IP is private, the entire resolution fails
        def mock_resolver(host: str, port: int) -> list[tuple[Any, ...]]:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port)),
            ]

        with pytest.raises(PolicyDeniedError, match="resolves to non-public or forbidden address"):
            resolve_and_validate_addresses("dual-homed.example.com", 80, resolver=mock_resolver)

    def test_resolution_to_cloud_metadata_fails(self) -> None:
        def mock_resolver(host: str, port: int) -> list[tuple[Any, ...]]:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port)),
            ]

        with pytest.raises(PolicyDeniedError, match="resolves to non-public or forbidden address"):
            resolve_and_validate_addresses("metadata.internal", 80, resolver=mock_resolver)

    def test_resolution_failure_raises_policy_denied(self) -> None:
        def failing_resolver(host: str, port: int) -> list[tuple[Any, ...]]:
            raise socket.gaierror(-2, "Name or service not known")

        with pytest.raises(PolicyDeniedError, match="could not be resolved"):
            resolve_and_validate_addresses(
                "nonexistent.example.internal", 80, resolver=failing_resolver
            )

    @pytest.mark.anyio
    async def test_validate_outbound_url_end_to_end(self) -> None:
        def mock_resolver(host: str, port: int) -> list[tuple[Any, ...]]:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            ]

        ips = await validate_outbound_url(
            "https://example.com/path",
            allowed_hosts=("example.com",),
            resolver=mock_resolver,
        )
        assert ips == ["93.184.216.34"]


class TestSafeNetworkBackend:
    """Test SafeNetworkBackend socket binding and TLS SNI preservation."""

    @pytest.mark.anyio
    async def test_connect_tcp_binds_to_validated_ip(self) -> None:
        def mock_resolver(host: str, port: int) -> list[tuple[Any, ...]]:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            ]

        backend = SafeNetworkBackend(
            allowed_hosts=("example.com",),
            dns_resolver=mock_resolver,
        )

        fake_stream = MagicMock()
        mock_start_tls = AsyncMock(return_value=fake_stream)
        fake_stream.start_tls = mock_start_tls

        connected_to: list[tuple[str, int]] = []

        async def mock_super_connect(backend_self: Any, ip: str, port: int, **kwargs: Any) -> Any:
            connected_to.append((ip, port))
            return fake_stream

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "httpcore.AnyIOBackend.connect_tcp",
                mock_super_connect,
            )
            stream = await backend.connect_tcp("example.com", 443)

        assert connected_to == [("93.184.216.34", 443)]

        # Verify TLS SNI wrapping: start_tls passes original server_hostname="example.com"
        mock_ssl_context = MagicMock()
        await stream.start_tls(ssl_context=mock_ssl_context)
        mock_start_tls.assert_awaited_once_with(
            mock_ssl_context,
            server_hostname="example.com",
            timeout=None,
        )

    @pytest.mark.anyio
    async def test_connect_tcp_blocks_rebinding_to_private(self) -> None:
        def mock_resolver(host: str, port: int) -> list[tuple[Any, ...]]:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
            ]

        backend = SafeNetworkBackend(
            dns_resolver=mock_resolver,
        )

        with pytest.raises(PolicyDeniedError, match="resolves to non-public or forbidden address"):
            await backend.connect_tcp("rebound.attacker.com", 80)

    @pytest.mark.anyio
    async def test_dns_rebinding_single_resolution_pinning(self) -> None:
        """Simulate DNS server that changes to 127.0.0.1 on second lookup.

        Verify that backend resolves once, pins the TCP socket to the validated IP,
        and never performs an uncontrolled second resolution.
        """
        dns_call_count = 0

        def shifting_resolver(host: str, port: int) -> list[tuple[Any, ...]]:
            nonlocal dns_call_count
            dns_call_count += 1
            if dns_call_count == 1:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

        connected_ips: list[str] = []
        fake_stream = MagicMock()
        fake_stream.start_tls = AsyncMock(return_value=fake_stream)

        async def mock_super_connect(backend_self: Any, ip: str, port: int, **kwargs: Any) -> Any:
            connected_ips.append(ip)
            return fake_stream

        backend = SafeNetworkBackend(
            allowed_hosts=("rebind.attacker.com",),
            dns_resolver=shifting_resolver,
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("httpcore.AnyIOBackend.connect_tcp", mock_super_connect)
            await backend.connect_tcp("rebind.attacker.com", 80)

        # DNS was resolved exactly once
        assert dns_call_count == 1
        # Connected directly to the validated public IP, never to 127.0.0.1
        assert connected_ips == ["93.184.216.34"]


class TestCallerIntegrations:
    """Test safe client integration in http_tool, remote_tool, and HTTPModelService."""

    @pytest.mark.anyio
    async def test_http_tool_denies_disallowed_host(self) -> None:
        tool = http_tool(allowed_hosts=("trusted.com",))
        context = ToolContext(
            run_id="run-1",
            step_id="step-1",
            user_id="user-1",
            tenant_id="tenant-1",
            idempotency_key="idem-1",
            permissions=frozenset({"network.http"}),
        )

        with pytest.raises(PolicyDeniedError, match="is not allowlisted"):
            await tool.execute({"url": "https://evil.com/data"}, context)

    @pytest.mark.anyio
    async def test_remote_tool_denies_disallowed_host(self) -> None:
        definition = ToolDefinition(
            name="custom.remote",
            version="1.0.0",
            description="Test remote tool",
            input_schema={},
            output_schema={},
            required_permissions=frozenset({"network.http"}),
            side_effect=SideEffect.EXTERNAL,
            idempotency=Idempotency.KEYED,
        )
        tool = remote_tool(
            definition,
            endpoint="https://internal.corp/exec",
            allowed_hosts=("api.corp",),
        )
        context = ToolContext(
            run_id="run-1",
            step_id="step-1",
            user_id="user-1",
            tenant_id="tenant-1",
            idempotency_key="idem-1",
            permissions=frozenset({"network.http"}),
        )

        with pytest.raises(PolicyDeniedError, match="is not allowlisted"):
            await tool.execute({}, context)

    @pytest.mark.anyio
    async def test_http_model_service_denies_disallowed_host(self) -> None:
        manifest = ModelServiceManifest(
            name="test-model",
            version="1.0.0",
            kind=ModelServiceKind.FORECAST,
            description="Remote model",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        )
        service = HTTPModelService(
            manifest=manifest,
            endpoint="https://untrusted.com/v1/chat",
            allowed_hosts=("models.internal",),
        )

        request = ModelServiceRequest(
            model="test-model",
            inputs=({"query": "Hello"},),
        )
        with pytest.raises(PolicyDeniedError, match="is not allowlisted"):
            await service.predict(request)

    @pytest.mark.anyio
    async def test_http_model_service_health_check_handles_blocked_host(self) -> None:
        manifest = ModelServiceManifest(
            name="test-model",
            version="1.0.0",
            kind=ModelServiceKind.FORECAST,
            description="Remote model",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        )
        service = HTTPModelService(
            manifest=manifest,
            endpoint="https://models.internal/v1/chat",
            health_endpoint="https://untrusted.com/healthz",
            allowed_hosts=("models.internal",),
        )

        health = await service.health()
        assert health.healthy is False
        assert "PolicyDeniedError" in (health.message or "")

    @pytest.mark.anyio
    async def test_create_safe_http_client_factory(self) -> None:
        client = create_safe_http_client(
            allowed_hosts=("api.github.com",),
            allow_private_networks=False,
        )
        assert isinstance(client, httpx.AsyncClient)
        await client.aclose()
