from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
import ssl
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlparse

import httpcore
import httpx

from algen_agent_runtime.exceptions.errors import ConfigurationError, PolicyDeniedError

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


def _matches_domain(hostname: str, pattern: str) -> bool:
    """``example.com`` matches itself and its subdomains; ``*`` matches every host; an IP
    network such as ``10.0.0.0/8`` matches IP literals inside it."""
    host = hostname.lower().strip("[]").rstrip(".")
    cleaned = pattern.lower().strip().lstrip("*").lstrip(".").rstrip(".")
    if pattern.strip() == "*":
        return True
    if not cleaned:
        return False
    try:
        network = ipaddress.ip_network(cleaned, strict=False)
    except ValueError:
        return host == cleaned or host.endswith("." + cleaned)
    try:
        return ipaddress.ip_address(host) in network
    except ValueError:
        return False


@dataclass(frozen=True, repr=False)
class EgressProxy:
    """The HTTP(S) proxy every outbound client sends through.

    ``url`` carries no credentials; ``username`` and ``password`` are sent as Basic
    ``Proxy-Authorization`` and never appear in ``repr`` or errors. ``https://`` targets go
    through a CONNECT tunnel, so TLS stays end to end; ``http://`` targets are forwarded.
    Hosts matching ``no_proxy`` are reached directly. With ``resolve_targets`` the Runtime
    resolves each target and refuses private or forbidden addresses before it contacts the
    proxy; without it (for hosts that cannot resolve external names) only the allowlist and IP
    literals are checked, and the proxy must refuse private destinations itself.
    """

    url: str
    username: str | None = None
    password: str | None = None
    no_proxy: tuple[str, ...] = ()
    resolve_targets: bool = True

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigurationError("the egress proxy URL must be an http:// or https:// URL")
        if parsed.username is not None or parsed.password is not None:
            raise ConfigurationError(
                "the egress proxy URL must not contain credentials; configure them as env:// "
                "references"
            )
        if (self.username is None) != (self.password is None):
            raise ConfigurationError("set the egress proxy username and password together")

    @property
    def host(self) -> str:
        return (urlparse(self.url).hostname or "").lower()

    @property
    def port(self) -> int:
        parsed = urlparse(self.url)
        return parsed.port or (443 if parsed.scheme == "https" else 80)

    def bypasses(self, host: str) -> bool:
        return any(_matches_domain(host, pattern) for pattern in self.no_proxy)

    def as_httpx(self) -> httpx.Proxy:
        auth = (self.username, self.password) if self.username is not None else None
        return httpx.Proxy(self.url, auth=cast(tuple[str, str] | None, auth))

    def __repr__(self) -> str:
        auth = "set" if self.username is not None else "none"
        return f"EgressProxy(url={self.url!r}, credentials={auth}, no_proxy={self.no_proxy!r})"


@dataclass(frozen=True, repr=False)
class RemoteIdentity:
    """TLS settings for one remote host, or ``*.domain`` for its subdomains.

    ``ssl_context`` replaces the default verification context for that host (a custom CA
    bundle, a client certificate for mTLS); ``pinned_sha256`` additionally requires the
    server's leaf certificate to have one of these SHA-256 fingerprints (lowercase hex).
    """

    pattern: str
    ssl_context: ssl.SSLContext | None = None
    pinned_sha256: frozenset[str] = frozenset()

    def matches(self, hostname: str) -> bool:
        host = hostname.lower().rstrip(".")
        pattern = self.pattern.lower().rstrip(".")
        if pattern.startswith("*."):
            return host.endswith(pattern[1:])
        return host == pattern

    def __repr__(self) -> str:
        return (
            f"RemoteIdentity(pattern={self.pattern!r}, custom_tls={self.ssl_context is not None}, "
            f"pins={len(self.pinned_sha256)})"
        )


@dataclass(frozen=True)
class OutboundNetwork:
    """Deployment-wide egress settings, resolved: the proxy and per-host TLS identities.

    Build it with ``algen_agent_runtime.security.outbound.build_outbound_network`` and pass it
    to ``create_safe_http_client(outbound=...)`` (guarded clients) or
    ``create_outbound_http_client`` (operator-configured endpoints such as model providers).
    """

    proxy: EgressProxy | None = None
    remotes: tuple[RemoteIdentity, ...] = ()

    @property
    def active(self) -> bool:
        return self.proxy is not None or bool(self.remotes)

    def remote_for(self, hostname: str) -> RemoteIdentity | None:
        exact = [remote for remote in self.remotes if not remote.pattern.startswith("*.")]
        wildcard = sorted(
            (remote for remote in self.remotes if remote.pattern.startswith("*.")),
            key=lambda remote: len(remote.pattern),
            reverse=True,
        )
        for remote in (*exact, *wildcard):
            if remote.matches(hostname):
                return remote
        return None


def certificate_fingerprint(der: bytes) -> str:
    """The SHA-256 fingerprint of a DER certificate, as lowercase hex."""
    return hashlib.sha256(der).hexdigest()


def _instrument_tls(
    stream: httpcore.AsyncNetworkStream,
    *,
    hostname: str,
    connected_ip: str | None,
    outbound: OutboundNetwork | None,
) -> httpcore.AsyncNetworkStream:
    """Make ``start_tls`` use the original host name for SNI and certificate checks, the
    host's configured TLS context, and its pinned fingerprints. Applies to TLS inside a TLS
    tunnel too (an https proxy)."""
    original_start_tls = stream.start_tls

    async def start_tls(
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        name = (
            hostname
            if server_hostname is None or server_hostname == connected_ip
            else server_hostname
        )
        remote = outbound.remote_for(name) if outbound is not None else None
        context = remote.ssl_context if remote and remote.ssl_context else ssl_context
        tls_stream = await original_start_tls(context, server_hostname=name, timeout=timeout)
        if remote is not None and remote.pinned_sha256:
            ssl_object = tls_stream.get_extra_info("ssl_object")
            der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
            if der is None or certificate_fingerprint(der) not in remote.pinned_sha256:
                await tls_stream.aclose()
                raise PolicyDeniedError(
                    f"the TLS certificate of {name!r} does not match its pinned fingerprints"
                )
        return _instrument_tls(tls_stream, hostname=name, connected_ip=None, outbound=outbound)

    stream.start_tls = start_tls  # type: ignore[method-assign]
    return stream


class OutboundNetworkBackend(httpcore.AnyIOBackend):
    """Network backend that applies per-host TLS identities without address checks; for
    endpoints the operator configured (model providers)."""

    def __init__(self, outbound: OutboundNetwork | None = None) -> None:
        super().__init__()
        self.outbound = outbound

    async def _open(
        self,
        hostname: str,
        address: str,
        port: int,
        timeout: float | None,  # noqa: ASYNC109
        local_address: str | None,
        socket_options: Any | None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await super().connect_tcp(
            address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        return _instrument_tls(
            stream,
            hostname=hostname,
            connected_ip=address if address != hostname else None,
            outbound=self.outbound,
        )

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Any | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._open(host, host, port, timeout, local_address, socket_options)


class SafeNetworkBackend(OutboundNetworkBackend):
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
        outbound: OutboundNetwork | None = None,
    ) -> None:
        super().__init__(outbound)
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
                return await self._open(host, ip, port, timeout, local_address, socket_options)
            except (OSError, httpcore.ConnectError) as exc:
                last_exc = exc
                continue

        if last_exc is not None:
            raise last_exc
        raise PolicyDeniedError(f"Could not connect to outbound host {host!r}")


class _ProxyNetworkBackend(OutboundNetworkBackend):
    """The proxied pool's backend: it connects to the configured proxy and nowhere else."""

    def __init__(self, outbound: OutboundNetwork, proxy: EgressProxy) -> None:
        super().__init__(outbound)
        self._proxy = proxy

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Any | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if (host.lower().strip("[]"), port) != (self._proxy.host.strip("[]"), self._proxy.port):
            raise PolicyDeniedError("the proxied connection pool connects only to the egress proxy")
        return await super().connect_tcp(host, port, timeout, local_address, socket_options)


class SafeAsyncTransport(httpx.AsyncHTTPTransport):
    """httpx transport configured with SafeNetworkBackend to eliminate DNS rebinding TOCTOU.

    With an egress proxy in ``outbound``, requests to hosts outside ``no_proxy`` are checked
    against the allowlist and private-network rules first and then sent through the proxy;
    the proxy makes the final connection, so DNS pinning protects only up to the proxy.
    ``guarded=False`` skips the address checks (operator-configured endpoints) but still
    applies the proxy and TLS identities.
    """

    def __init__(
        self,
        allowed_hosts: tuple[str, ...] = (),
        allow_private_networks: bool = False,
        dns_resolver: DNSResolver | None = None,
        *,
        outbound: OutboundNetwork | None = None,
        guarded: bool = True,
        **kwargs: Any,
    ) -> None:
        if kwargs.pop("proxy", None) is not None:
            raise PolicyDeniedError(
                "SafeAsyncTransport takes its proxy from the deployment's network.egress_proxy"
            )
        super().__init__(**kwargs)
        self.allowed_hosts = allowed_hosts
        self.allow_private_networks = allow_private_networks
        self.dns_resolver = dns_resolver
        self.outbound = outbound
        self.guarded = guarded
        self.safe_backend: OutboundNetworkBackend = (
            SafeNetworkBackend(
                allowed_hosts=allowed_hosts,
                allow_private_networks=allow_private_networks,
                dns_resolver=dns_resolver,
                outbound=outbound,
            )
            if guarded
            else OutboundNetworkBackend(outbound)
        )
        if hasattr(self._pool, "_network_backend"):
            self._pool._network_backend = self.safe_backend
        self._proxy = outbound.proxy if outbound is not None else None
        self._proxied: httpx.AsyncHTTPTransport | None = None
        if outbound is not None and self._proxy is not None:
            self._proxied = httpx.AsyncHTTPTransport(proxy=self._proxy.as_httpx(), **kwargs)
            self._proxied._pool._network_backend = _ProxyNetworkBackend(outbound, self._proxy)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._proxied is None or self._proxy is None or self._proxy.bypasses(request.url.host):
            return await super().handle_async_request(request)
        if self.guarded:
            await self._check_proxied_target(request.url)
        return await self._proxied.handle_async_request(request)

    async def _check_proxied_target(self, url: httpx.URL) -> None:
        """The checks a direct connection makes, before anything is sent to the proxy."""
        host = url.host
        port = url.port or (443 if url.scheme == "https" else 80)
        assert self._proxy is not None
        if self._proxy.resolve_targets:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: resolve_and_validate_addresses(
                    host,
                    port,
                    allowed_hosts=self.allowed_hosts,
                    allow_private_networks=self.allow_private_networks,
                    resolver=self.dns_resolver,
                ),
            )
            return
        check_host_allowlist(host, self.allowed_hosts)
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            return
        if not is_ip_allowed(literal, self.allow_private_networks):
            raise PolicyDeniedError(
                f"outbound destination {host!r} is a non-public or forbidden address"
            )

    async def aclose(self) -> None:
        await super().aclose()
        if self._proxied is not None:
            await self._proxied.aclose()


_TRANSPORT_KEYS = frozenset(
    {
        "verify",
        "cert",
        "trust_env",
        "http1",
        "http2",
        "limits",
        "uds",
        "local_address",
        "retries",
        "socket_options",
    }
)


def _check_caller_proxy(proxy: Any, outbound: OutboundNetwork | None) -> None:
    """A caller's ``proxy=`` is accepted only when it names the configured egress proxy."""
    configured = outbound.proxy if outbound is not None else None
    url = proxy.url if isinstance(proxy, httpx.Proxy) else httpx.URL(str(proxy))
    if configured is not None:
        expected = httpx.URL(configured.url)
        if (url.scheme, url.host, url.port or None) == (
            expected.scheme,
            expected.host,
            expected.port or None,
        ):
            return
    raise PolicyDeniedError(
        "outbound clients use only the deployment's network.egress_proxy; a different proxy "
        "would bypass the address checks"
    )


def create_safe_http_client(
    allowed_hosts: tuple[str, ...] = (),
    allow_private_networks: bool = False,
    dns_resolver: DNSResolver | None = None,
    timeout: float | httpx.Timeout | None = 30.0,
    follow_redirects: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    outbound: OutboundNetwork | None = None,
    **client_kwargs: Any,
) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient equipped with DNS rebinding and SSRF protection.

    ``outbound`` applies the deployment's egress proxy and per-host TLS identities. A
    ``proxy`` keyword is accepted only when it is the configured egress proxy (it is then
    applied through ``outbound``); ``mounts`` is refused, since it would bypass the checks.
    """
    proxy = client_kwargs.pop("proxy", None)
    if proxy is not None:
        _check_caller_proxy(proxy, outbound)
    if client_kwargs.get("mounts"):
        raise PolicyDeniedError("create_safe_http_client does not accept mounts")
    client_kwargs.pop("mounts", None)
    if transport is None:
        transport_kwargs = {k: v for k, v in client_kwargs.items() if k in _TRANSPORT_KEYS}
        remaining_client_kwargs = {
            k: v for k, v in client_kwargs.items() if k not in _TRANSPORT_KEYS
        }
        transport = SafeAsyncTransport(
            allowed_hosts=allowed_hosts,
            allow_private_networks=allow_private_networks,
            dns_resolver=dns_resolver,
            outbound=outbound,
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


def create_outbound_http_client(
    outbound: OutboundNetwork | None = None, **client_kwargs: Any
) -> httpx.AsyncClient:
    """A client for an operator-configured endpoint (a model provider's base_url).

    Without an active ``outbound`` this is a plain ``httpx.AsyncClient``, exactly as before.
    With one, it sends through the egress proxy and applies TLS identities; it makes no
    allowlist or private-network checks, since the operator chose the endpoint.
    """
    if outbound is None or not outbound.active:
        return httpx.AsyncClient(**client_kwargs)
    transport_kwargs = {k: v for k, v in client_kwargs.items() if k in _TRANSPORT_KEYS}
    remaining = {k: v for k, v in client_kwargs.items() if k not in _TRANSPORT_KEYS}
    transport = SafeAsyncTransport(outbound=outbound, guarded=False, **transport_kwargs)
    return httpx.AsyncClient(transport=transport, **remaining)


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
