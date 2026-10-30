"""Egress proxy and remote TLS identity (mTLS, custom CA, pinned certificates)."""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
import socket
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.exceptions.errors import ConfigurationError, PolicyDeniedError
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.security.network import (
    EgressProxy,
    OutboundNetwork,
    SafeAsyncTransport,
    certificate_fingerprint,
    create_safe_http_client,
)
from algen_agent_runtime.security.outbound import NetworkSettings, build_outbound_network

PROXY_USER = "proxy-user"
PROXY_PASSWORD = "pp-" + "x" * 12  # a throwaway test value

PUBLIC = {"api.example.com": "93.184.216.34", "internal.example.com": "10.0.0.5"}


def resolver(host: str, port: int) -> list[tuple[Any, ...]]:
    address = PUBLIC.get(host, "127.0.0.1")
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65_536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


class ProxyStub:
    """A minimal HTTP proxy: CONNECT tunnels to ``upstream``; other requests are answered."""

    def __init__(self, upstream: tuple[str, int] | None = None) -> None:
        self.upstream = upstream
        self.connections = 0
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.port = 0
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> ProxyStub:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        lines = head.split("\r\n")
        method, target, _ = lines[0].split(" ")
        headers = dict(line.split(": ", 1) for line in lines[1:] if line)
        self.requests.append((f"{method} {target}", {k.lower(): v for k, v in headers.items()}))
        if method == "CONNECT" and self.upstream is not None:
            up_reader, up_writer = await asyncio.open_connection(*self.upstream)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
            return
        body = b"forwarded"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()


async def _http_server(
    handler: Any, context: ssl.SSLContext | None = None
) -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=context)
    return server, server.sockets[0].getsockname()[1]


async def _answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.readuntil(b"\r\n\r\n")
    peer = writer.get_extra_info("peercert") or {}
    subject = dict(item[0] for item in peer.get("subject", ()))
    body = json.dumps({"client": subject.get("commonName")}).encode()
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    await writer.drain()
    writer.close()


def _proxy(url: str, **kwargs: Any) -> OutboundNetwork:
    return OutboundNetwork(proxy=EgressProxy(url=url, **kwargs))


# -- egress proxy -------------------------------------------------------------------------


async def test_http_target_is_forwarded_through_the_proxy() -> None:
    async with ProxyStub() as proxy:
        async with create_safe_http_client(
            allowed_hosts=("api.example.com",),
            dns_resolver=resolver,
            outbound=_proxy(proxy.url),
        ) as client:
            response = await client.get("http://api.example.com/items")
    assert response.text == "forwarded"
    assert proxy.requests[0][0] == "GET http://api.example.com/items"


@pytest.mark.parametrize(
    "url",
    [
        "https://elsewhere.example.org/",  # not allowlisted
        "https://internal.example.com/",  # allowlisted, resolves to a private address
        "https://127.0.0.1/",  # private IP literal
        "http://169.254.169.254/latest/meta-data/",  # metadata service
    ],
)
async def test_refused_targets_never_reach_the_proxy(url: str) -> None:
    async with ProxyStub() as proxy:
        async with create_safe_http_client(
            allowed_hosts=(
                "api.example.com",
                "internal.example.com",
                "127.0.0.1",
                "169.254.169.254",
            ),
            dns_resolver=resolver,
            outbound=_proxy(proxy.url),
        ) as client:
            with pytest.raises(PolicyDeniedError):
                await client.get(url)
    assert proxy.connections == 0


async def test_unresolved_targets_still_check_the_allowlist_and_literals() -> None:
    async with ProxyStub() as proxy:
        outbound = _proxy(proxy.url, resolve_targets=False)
        async with create_safe_http_client(
            allowed_hosts=("api.example.com", "10.0.0.1"), outbound=outbound
        ) as client:
            with pytest.raises(PolicyDeniedError):
                await client.get("http://other.example.org/")
            with pytest.raises(PolicyDeniedError):
                await client.get("http://10.0.0.1/")
            assert proxy.connections == 0
            # A name is left to the proxy to resolve.
            assert (await client.get("http://api.example.com/")).text == "forwarded"
    assert proxy.connections == 1


async def test_no_proxy_hosts_connect_directly() -> None:
    server, port = await _http_server(_answer)
    try:
        async with ProxyStub() as proxy:
            async with create_safe_http_client(
                allow_private_networks=True,
                outbound=_proxy(proxy.url, no_proxy=("127.0.0.0/8",)),
            ) as client:
                response = await client.get(f"http://127.0.0.1:{port}/")
        assert response.status_code == 200
        assert proxy.connections == 0
    finally:
        server.close()


async def test_a_caller_proxy_must_be_the_configured_one() -> None:
    with pytest.raises(PolicyDeniedError, match="egress_proxy"):
        create_safe_http_client(proxy="http://elsewhere.example.org:3128")
    configured = _proxy("http://proxy.internal:3128")
    with pytest.raises(PolicyDeniedError):
        create_safe_http_client(proxy="http://other.internal:3128", outbound=configured)
    client = create_safe_http_client(proxy="http://proxy.internal:3128", outbound=configured)
    await client.aclose()
    with pytest.raises(PolicyDeniedError):
        create_safe_http_client(mounts={"all://": httpx.AsyncHTTPTransport()})
    with pytest.raises(PolicyDeniedError):
        SafeAsyncTransport(proxy="http://elsewhere.example.org:3128")


async def test_proxy_credentials_are_sent_but_never_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    environment = {"PROXY_USER": PROXY_USER, "PROXY_PASSWORD": PROXY_PASSWORD}
    async with ProxyStub() as proxy:
        outbound = build_outbound_network(
            NetworkSettings.model_validate(
                {
                    "egress_proxy": {
                        "url": proxy.url,
                        "username": "env://PROXY_USER",
                        "password": "env://PROXY_PASSWORD",
                    }
                }
            ),
            environment,
        )
        assert outbound is not None
        async with create_safe_http_client(
            allowed_hosts=("api.example.com",), dns_resolver=resolver, outbound=outbound
        ) as client:
            await client.get("http://api.example.com/")
            with pytest.raises(PolicyDeniedError) as refused:
                await client.get("http://elsewhere.example.org/")
    expected = base64.b64encode(f"{PROXY_USER}:{PROXY_PASSWORD}".encode()).decode()
    assert proxy.requests[0][1]["proxy-authorization"] == f"Basic {expected}"
    for text in (caplog.text, repr(outbound), str(refused.value), expected):
        assert PROXY_PASSWORD not in text


async def test_an_unreachable_proxy_error_has_no_credentials() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    outbound = _proxy(f"http://127.0.0.1:{port}", username=PROXY_USER, password=PROXY_PASSWORD)
    async with create_safe_http_client(
        allowed_hosts=("api.example.com",), dns_resolver=resolver, outbound=outbound
    ) as client:
        with pytest.raises(httpx.ConnectError) as failed:
            await client.get("http://api.example.com/")
    assert PROXY_PASSWORD not in str(failed.value)


def test_proxy_settings_keep_credentials_out_of_the_url() -> None:
    with pytest.raises(ValueError, match="credentials"):
        NetworkSettings.model_validate(
            {"egress_proxy": {"url": "http://user:secret@proxy.internal:3128"}}
        )
    with pytest.raises(ValueError, match="env://"):
        NetworkSettings.model_validate(
            {
                "egress_proxy": {
                    "url": "http://proxy.internal:3128",
                    "username": "user",
                    "password": "env://P",
                }
            }
        )
    outbound = build_outbound_network(
        NetworkSettings.model_validate({"egress_proxy": {"url": "env://EGRESS_URL"}}),
        {"EGRESS_URL": f"http://{PROXY_USER}:{PROXY_PASSWORD}@proxy.internal:3128"},
    )
    assert outbound is not None and outbound.proxy is not None
    assert outbound.proxy.url == "http://proxy.internal:3128"
    assert outbound.proxy.password == PROXY_PASSWORD
    assert PROXY_PASSWORD not in repr(outbound)
    assert build_outbound_network(NetworkSettings()) is None


def test_model_providers_use_the_outbound_client_only_when_configured() -> None:
    def provider_transport(settings: dict[str, Any]) -> Any:
        container = build_container(
            AppSettings.model_validate(
                {"providers": {"llm": {"type": "openai", "default_model": "m"}}, **settings}
            )
        )
        provider = container.runtime.router.provider("llm")
        return provider._client._transport  # type: ignore[attr-defined]

    assert not isinstance(provider_transport({}), SafeAsyncTransport)
    transport = provider_transport(
        {"network": {"egress_proxy": {"url": "http://proxy.internal:3128"}}}
    )
    assert isinstance(transport, SafeAsyncTransport) and transport.guarded is False


# -- remote TLS identity ------------------------------------------------------------------


@dataclass
class Pki:
    ca: Path
    server_cert: Path
    server_key: Path
    client_ca: Path
    client_cert_pem: str
    client_key_pem: str
    server_der: bytes


def _pki(directory: Path) -> Pki:
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.UTC)

    def name(common: str) -> x509.Name:
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])

    def issue(
        subject: str,
        issuer: tuple[x509.Name, Any] | None,
        *,
        ca: bool,
        dns: str | None = None,
    ) -> tuple[x509.Certificate, Any]:
        key = ec.generate_private_key(ec.SECP256R1())
        issuer_name, issuer_key = issuer or (name(subject), key)
        builder = (
            x509.CertificateBuilder()
            .subject_name(name(subject))
            .issuer_name(issuer_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        )
        if ca:
            builder = builder.add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            ).add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
        else:
            builder = builder.add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
                critical=False,
            )
        if dns:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.DNSName(dns)]), critical=False
            )
        return builder.sign(issuer_key, hashes.SHA256()), key

    def pem(certificate: x509.Certificate) -> bytes:
        return certificate.public_bytes(serialization.Encoding.PEM)

    def key_pem(key: Any) -> bytes:
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    ca, ca_key = issue("Test Server CA", None, ca=True)
    server, server_key = issue(
        "api.example.com", (ca.subject, ca_key), ca=False, dns="api.example.com"
    )
    client_ca, client_ca_key = issue("Test Client CA", None, ca=True)
    client, client_key = issue("runtime-client", (client_ca.subject, client_ca_key), ca=False)
    files = {
        "ca.pem": pem(ca),
        "server.pem": pem(server),
        "server-key.pem": key_pem(server_key),
        "client-ca.pem": pem(client_ca),
    }
    for filename, content in files.items():
        (directory / filename).write_bytes(content)
    return Pki(
        ca=directory / "ca.pem",
        server_cert=directory / "server.pem",
        server_key=directory / "server-key.pem",
        client_ca=directory / "client-ca.pem",
        client_cert_pem=pem(client).decode(),
        client_key_pem=key_pem(client_key).decode(),
        server_der=server.public_bytes(serialization.Encoding.DER),
    )


@pytest.fixture
async def mtls_server(tmp_path: Path) -> AsyncIterator[tuple[Pki, int]]:
    pki = _pki(tmp_path)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(pki.server_cert, pki.server_key)
    context.load_verify_locations(pki.client_ca)
    context.verify_mode = ssl.CERT_REQUIRED
    server, port = await _http_server(_answer, context)
    yield pki, port
    server.close()


def _identity(pki: Pki, **extra: Any) -> tuple[NetworkSettings, dict[str, str]]:
    remote = {
        "ca_bundle": str(pki.ca),
        "client_certificate": "env://CLIENT_CERT",
        "client_key": "env://CLIENT_KEY",
        **extra,
    }
    environment = {"CLIENT_CERT": pki.client_cert_pem, "CLIENT_KEY": pki.client_key_pem}
    return NetworkSettings.model_validate({"remotes": {"api.example.com": remote}}), environment


def _local_resolver(host: str, port: int) -> list[tuple[Any, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]


async def _get(outbound: OutboundNetwork | None, port: int) -> httpx.Response:
    async with create_safe_http_client(
        allowed_hosts=("api.example.com",),
        allow_private_networks=True,
        dns_resolver=_local_resolver,
        outbound=outbound,
    ) as client:
        return await client.get(f"https://api.example.com:{port}/")


async def test_mtls_presents_the_client_certificate(
    mtls_server: tuple[Pki, int], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    pki, port = mtls_server
    settings, environment = _identity(pki)
    response = await _get(build_outbound_network(settings, environment), port)
    assert response.json() == {"client": "runtime-client"}
    assert "PRIVATE KEY" not in caplog.text


async def test_without_the_identity_the_handshake_fails(mtls_server: tuple[Pki, int]) -> None:
    pki, port = mtls_server
    with pytest.raises(httpx.ConnectError):
        await _get(None, port)  # the private CA is unknown
    trust_only = NetworkSettings.model_validate(
        {"remotes": {"api.example.com": {"ca_bundle": str(pki.ca)}}}
    )
    with pytest.raises((httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)):
        await _get(build_outbound_network(trust_only), port)  # no client certificate


async def test_pinned_certificates(mtls_server: tuple[Pki, int]) -> None:
    pki, port = mtls_server
    fingerprint = certificate_fingerprint(pki.server_der)
    colons = ":".join(fingerprint[i : i + 2] for i in range(0, 64, 2)).upper()
    settings, environment = _identity(pki, pinned_sha256=[colons])
    response = await _get(build_outbound_network(settings, environment), port)
    assert response.status_code == 200
    settings, environment = _identity(pki, pinned_sha256=["ab" * 32])
    with pytest.raises(PolicyDeniedError, match="pinned"):
        await _get(build_outbound_network(settings, environment), port)


async def test_mtls_through_the_proxy_tunnel(mtls_server: tuple[Pki, int]) -> None:
    pki, port = mtls_server
    settings, environment = _identity(pki, pinned_sha256=[certificate_fingerprint(pki.server_der)])
    identity = build_outbound_network(settings, environment)
    assert identity is not None
    async with ProxyStub(upstream=("127.0.0.1", port)) as proxy:
        outbound = OutboundNetwork(proxy=EgressProxy(url=proxy.url), remotes=identity.remotes)
        async with create_safe_http_client(
            allowed_hosts=("api.example.com",), dns_resolver=resolver, outbound=outbound
        ) as client:
            response = await client.get("https://api.example.com/")
    assert response.json() == {"client": "runtime-client"}
    assert proxy.requests[0][0] == "CONNECT api.example.com:443"


def test_identity_settings_refuse_inline_secrets(tmp_path: Path) -> None:
    pki = _pki(tmp_path)
    with pytest.raises(ValueError, match="not PEM text"):
        NetworkSettings.model_validate(
            {
                "remotes": {
                    "api.example.com": {
                        "client_certificate": "env://C",
                        "client_key": pki.client_key_pem,
                    }
                }
            }
        )
    with pytest.raises(ValueError, match="together"):
        NetworkSettings.model_validate({"remotes": {"api.example.com": {"client_key": "env://K"}}})
    with pytest.raises(ValueError, match="64 hex"):
        NetworkSettings.model_validate({"remotes": {"api.example.com": {"pinned_sha256": ["x"]}}})
    with pytest.raises(ValueError, match="host name"):
        NetworkSettings.model_validate({"remotes": {"https://api.example.com": {}}})


def test_a_bad_key_error_does_not_echo_the_key(tmp_path: Path) -> None:
    pki = _pki(tmp_path)
    settings, _ = _identity(pki)
    wrong = pki.server_key.read_text()  # a valid key that does not match the certificate
    with pytest.raises(ConfigurationError) as failed:
        build_outbound_network(settings, {"CLIENT_CERT": pki.client_cert_pem, "CLIENT_KEY": wrong})
    assert "PRIVATE KEY" not in str(failed.value)
    assert failed.value.__cause__ is None
