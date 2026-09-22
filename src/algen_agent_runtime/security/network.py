from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import urllib.parse
from collections.abc import Callable
from typing import Any, cast
from urllib.parse import urlparse

import httpcore
import httpx

from algen_agent_runtime.exceptions.errors import PolicyDeniedError

# Explicit list of known cloud metadata and sensitive link-local / private endpoints
FORBIDDEN_METADATA_IPS: frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address] = frozenset(
    [
        ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Azure/OpenStack metadata
        ipaddress.ip_address("169.254.170.2"),  # AWS ECS container task metadata
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IMDSv6
        ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud metadata
    ]
)

DNSResolver = Callable[[str, int], list[tuple[Any, ...]]]


def is_ip_allowed(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    allow_private_networks: bool = False,
) -> bool:
    """Determine whether an IP address is permitted for outbound connections.

    Rejects loopback, private, link-local, reserved, multicast, unspecified,
    and cloud metadata IP addresses unless allow_private_networks is explicitly enabled.
    """
    if allow_private_networks:
        return True

    # Unpack IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1) and evaluate mapped IPv4
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return is_ip_allowed(ip.ipv4_mapped, allow_private_networks=False)

    if ip in FORBIDDEN_METADATA_IPS:
        return False

    if not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local:
        return False

    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return False

    return True


def check_host_allowlist(hostname: str, allowed_hosts: tuple[str, ...]) -> None:
    """Verify that a hostname satisfies the configured host allowlist."""
    cleaned = hostname.lower().strip(".")
    if not allowed_hosts:
        return
    for host in allowed_hosts:
        norm_host = host.lower().strip(".")
        if cleaned == norm_host or cleaned.endswith("." + norm_host):
            return
    raise PolicyDeniedError(f"outbound host {hostname!r} is not allowlisted")


def normalize_and_validate_url(url: str) -> tuple[str, int]:
    """Validate URL syntax, scheme, and extract normalized ascii hostname and port.

    Rejects non-HTTP/HTTPS schemes, missing hosts, and embedded user credentials.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise PolicyDeniedError(
            f"outbound URL scheme {parsed.scheme!r} is not allowed (must be http or https)"
        )
    if not parsed.hostname:
        raise PolicyDeniedError("outbound URL must use http or https and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise PolicyDeniedError("outbound URL must not contain embedded user credentials")

    raw_hostname = parsed.hostname
    if "%" in raw_hostname:
        unquoted = urllib.parse.unquote(raw_hostname)
        if any(c in unquoted for c in ("%", "/", "@", ":")):
            raise PolicyDeniedError(
                f"outbound host {raw_hostname!r} contains invalid encoded characters"
            )
        raw_hostname = unquoted

    cleaned = raw_hostname.lower().rstrip(".")
    if not cleaned:
        raise PolicyDeniedError("outbound host cannot be empty")

    try:
        ascii_host = cleaned.encode("idna").decode("ascii")
    except Exception as exc:
        raise PolicyDeniedError(
            f"outbound host {cleaned!r} is not a valid internationalized domain name"
        ) from exc

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return ascii_host, port


def resolve_and_validate_addresses(
    hostname: str,
    port: int,
    allowed_hosts: tuple[str, ...] = (),
    allow_private_networks: bool = False,
    resolver: DNSResolver | None = None,
) -> list[str]:
    """Resolve DNS records once and validate all candidate IP addresses against SSRF policies.

    Fails closed if ANY candidate address resolves to a non-public/forbidden IP.
    Returns a list of validated IP address strings for direct socket binding.
    """
    check_host_allowlist(hostname, allowed_hosts)

    # Check direct IP literal
    try:
        ip_lit = ipaddress.ip_address(hostname)
        if not is_ip_allowed(ip_lit, allow_private_networks):
            raise PolicyDeniedError(
                f"outbound destination {hostname!r} is a non-public or forbidden address"
            )
        return [str(ip_lit)]
    except ValueError:
        pass

    try:
        if resolver is not None:
            addrinfo = resolver(hostname, port)
        else:
            addrinfo = socket.getaddrinfo(
                hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            )
    except socket.gaierror as exc:
        raise PolicyDeniedError(f"outbound host {hostname!r} could not be resolved") from exc

    if not addrinfo:
        raise PolicyDeniedError(f"outbound host {hostname!r} returned no address records")

    validated_ips: list[str] = []
    for entry in addrinfo:
        sockaddr = entry[4]
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            raise PolicyDeniedError(
                f"outbound host {hostname!r} resolved to invalid IP {ip_str!r}"
            ) from None

        if not is_ip_allowed(ip, allow_private_networks):
            raise PolicyDeniedError(
                f"outbound host {hostname!r} resolves to non-public or forbidden address {ip_str!r}"
            )
        if ip_str not in validated_ips:
            validated_ips.append(ip_str)

    return validated_ips


class SafeNetworkBackend(httpcore.AnyIOBackend):
    """Network backend that connects directly to pre-validated IP addresses.

    Eliminates DNS rebinding TOCTOU by resolving addresses once, validating all
    candidates, and binding the TCP socket directly to a validated IP, while
    preserving the original requested hostname for TLS SNI and certificate checks.
    """

    def __init__(
        self,
        allowed_hosts: tuple[str, ...] = (),
        allow_private_networks: bool = False,
        dns_resolver: DNSResolver | None = None,
    ) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts
        self.allow_private_networks = allow_private_networks
        self.dns_resolver = dns_resolver

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Any | None = None,
    ) -> httpcore.AsyncNetworkStream:
        loop = asyncio.get_running_loop()
        validated_ips = await loop.run_in_executor(
            None,
            lambda: resolve_and_validate_addresses(
                host,
                port,
                allowed_hosts=self.allowed_hosts,
                allow_private_networks=self.allow_private_networks,
                resolver=self.dns_resolver,
            ),
        )

        last_exc: Exception | None = None
        for ip in validated_ips:
            try:
                stream = await super().connect_tcp(
                    ip,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )

                # Wrap start_tls to ensure TLS SNI and server certificate validation use original host
                original_start_tls = stream.start_tls
                target_ip = ip

                async def start_tls_with_sni(
                    ssl_context: ssl.SSLContext,
                    server_hostname: str | None = None,
                    timeout: float | None = None,  # noqa: ASYNC109
                    *,
                    _orig: Callable[..., Any] = original_start_tls,
                    _ip: str = target_ip,
                ) -> httpcore.AsyncNetworkStream:
                    effective_hostname = (
                        host
                        if server_hostname is None or server_hostname == _ip
                        else server_hostname
                    )
                    tls_stream = await _orig(
                        ssl_context,
                        server_hostname=effective_hostname,
                        timeout=timeout,
                    )
                    return cast(httpcore.AsyncNetworkStream, tls_stream)

                stream.start_tls = start_tls_with_sni  # type: ignore[method-assign]
                return stream
            except (OSError, httpcore.ConnectError) as exc:
                last_exc = exc
                continue

        if last_exc is not None:
            raise last_exc
        raise PolicyDeniedError(f"Could not connect to outbound host {host!r}")


class SafeAsyncTransport(httpx.AsyncHTTPTransport):
    """httpx transport configured with SafeNetworkBackend to eliminate DNS rebinding TOCTOU."""

    def __init__(
        self,
        allowed_hosts: tuple[str, ...] = (),
        allow_private_networks: bool = False,
        dns_resolver: DNSResolver | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.safe_backend = SafeNetworkBackend(
            allowed_hosts=allowed_hosts,
            allow_private_networks=allow_private_networks,
            dns_resolver=dns_resolver,
        )
        if hasattr(self._pool, "_network_backend"):
            self._pool._network_backend = self.safe_backend


def create_safe_http_client(
    allowed_hosts: tuple[str, ...] = (),
    allow_private_networks: bool = False,
    dns_resolver: DNSResolver | None = None,
    timeout: float | httpx.Timeout | None = 30.0,
    follow_redirects: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    **client_kwargs: Any,
) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient equipped with DNS rebinding and SSRF protection."""
    if transport is None:
        transport_keys = {
            "verify",
            "cert",
            "trust_env",
            "http1",
            "http2",
            "limits",
            "proxy",
            "uds",
            "local_address",
            "retries",
            "socket_options",
        }
        transport_kwargs = {k: v for k, v in client_kwargs.items() if k in transport_keys}
        remaining_client_kwargs = {
            k: v for k, v in client_kwargs.items() if k not in transport_keys
        }
        transport = SafeAsyncTransport(
            allowed_hosts=allowed_hosts,
            allow_private_networks=allow_private_networks,
            dns_resolver=dns_resolver,
            **transport_kwargs,
        )
    else:
        remaining_client_kwargs = client_kwargs

    return httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        follow_redirects=follow_redirects,
        **remaining_client_kwargs,
    )


async def validate_outbound_url(
    url: str,
    allowed_hosts: tuple[str, ...] = (),
    allow_private_networks: bool = False,
    resolver: DNSResolver | None = None,
) -> list[str]:
    """Validate outbound URL scheme, host allowlist, and resolve/validate destination addresses."""
    hostname, port = normalize_and_validate_url(url)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: resolve_and_validate_addresses(
            hostname,
            port,
            allowed_hosts=allowed_hosts,
            allow_private_networks=allow_private_networks,
            resolver=resolver,
        ),
    )
