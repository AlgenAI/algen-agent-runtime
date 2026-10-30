"""Deployment settings for outbound connections: the egress proxy and remote TLS identities.

``network.egress_proxy`` sends every client the Runtime builds (HTTP tools, MCP HTTP
transports and OAuth, OIDC discovery and JWKS, workflow notifications, HTTP model services and
model providers) through one HTTP(S) proxy. ``network.remotes`` sets, per host, a custom CA
bundle, a client certificate for mTLS and pinned server certificate fingerprints.

Secrets (proxy credentials, client keys and their passwords) are ``env://`` references. A
certificate or key may also be a file path; a key given through ``env://`` is written to a
private temporary file only for as long as ``ssl`` needs to load it.
"""

from __future__ import annotations

import os
import re
import shutil
import ssl
import tempfile
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.security.network import EgressProxy, OutboundNetwork, RemoteIdentity

_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EgressProxySettings(_Strict):
    url: str = Field(
        description="The proxy, http://host:port or https://host:port, without credentials; "
        "or an env:// reference to it."
    )
    username: str | None = Field(default=None, description="env:// reference to the user name")
    password: str | None = Field(default=None, description="env:// reference to the password")
    no_proxy: tuple[str, ...] = Field(
        default=(),
        description="Hosts reached directly: example.com (and its subdomains), an IP network "
        "such as 10.0.0.0/8, or '*'.",
    )
    resolve_targets: bool = Field(
        default=True,
        description="Resolve each target and refuse private or forbidden addresses before "
        "contacting the proxy. Turn off only where the Runtime cannot resolve external names; "
        "the proxy must then refuse private destinations itself.",
    )

    @model_validator(mode="after")
    def validate_proxy(self) -> EgressProxySettings:
        if not self.url.startswith("env://"):
            parsed = urlparse(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("network.egress_proxy.url must be an http:// or https:// URL")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError(
                    "network.egress_proxy.url must not contain credentials; use username and "
                    "password env:// references"
                )
        for name in ("username", "password"):
            value = getattr(self, name)
            if value is not None and not value.startswith("env://"):
                raise ValueError(f"network.egress_proxy.{name} must be an env:// reference")
        if (self.username is None) != (self.password is None):
            raise ValueError("set network.egress_proxy username and password together")
        return self


class RemoteTlsSettings(_Strict):
    """How the Runtime authenticates one remote service and proves its own identity to it."""

    ca_bundle: str | None = Field(
        default=None,
        description="PEM CA bundle that replaces the default trust store for this host: a file "
        "path or an env:// reference to the PEM text.",
    )
    client_certificate: str | None = Field(
        default=None,
        description="PEM client certificate (chain) for mTLS: a file path or env:// reference.",
    )
    client_key: str | None = Field(
        default=None,
        description="PEM private key of the client certificate: a file path or env:// reference.",
    )
    client_key_password: str | None = Field(
        default=None, description="env:// reference to the key's password, if it is encrypted"
    )
    pinned_sha256: tuple[str, ...] = Field(
        default=(),
        description="SHA-256 fingerprints of accepted server leaf certificates (hex, colons "
        "optional). Checked in addition to normal certificate verification.",
    )

    @model_validator(mode="after")
    def validate_identity(self) -> RemoteTlsSettings:
        for name in ("ca_bundle", "client_certificate", "client_key"):
            value = getattr(self, name)
            if value is not None and "-----BEGIN" in value:
                raise ValueError(f"{name} must be a file path or an env:// reference, not PEM text")
        if (self.client_certificate is None) != (self.client_key is None):
            raise ValueError("set client_certificate and client_key together")
        if self.client_key_password is not None:
            if not self.client_key_password.startswith("env://"):
                raise ValueError("client_key_password must be an env:// reference")
            if self.client_key is None:
                raise ValueError("client_key_password needs client_key")
        pins = tuple(pin.replace(":", "").strip().lower() for pin in self.pinned_sha256)
        if any(not _FINGERPRINT.match(pin) for pin in pins):
            raise ValueError("pinned_sha256 entries must be 64 hex digits (colons optional)")
        object.__setattr__(self, "pinned_sha256", pins)
        return self


class NetworkSettings(_Strict):
    egress_proxy: EgressProxySettings | None = None
    remotes: dict[str, RemoteTlsSettings] = Field(
        default_factory=dict,
        description="TLS identity per host: 'api.example.com' or '*.example.com' (subdomains).",
    )

    @model_validator(mode="after")
    def validate_remotes(self) -> NetworkSettings:
        for pattern in self.remotes:
            bare = pattern.removeprefix("*.")
            if not bare or "*" in bare or "/" in bare or ":" in bare.strip("[]"):
                raise ValueError(
                    f"network.remotes key {pattern!r} must be a host name or '*.domain'"
                )
        return self


def _resolve(reference: str, field: str, environment: Mapping[str, str]) -> str:
    variable = reference.removeprefix("env://")
    value = environment.get(variable) or os.getenv(variable)
    if not value:
        raise ConfigurationError(f"{field}: required environment variable {variable!r} is not set")
    return value


def _material(
    reference: str, field: str, environment: Mapping[str, str]
) -> tuple[str | None, str | None]:
    """(file path, PEM text): exactly one is set."""
    if reference.startswith("env://"):
        return None, _resolve(reference, field, environment)
    path = Path(reference).expanduser()
    if not path.is_file():
        raise ConfigurationError(f"{field}: file {reference!r} does not exist")
    return str(path), None


def _ssl_context(
    pattern: str, remote: RemoteTlsSettings, environment: Mapping[str, str]
) -> ssl.SSLContext:
    field = f"network.remotes[{pattern!r}]"
    if remote.ca_bundle is not None:
        ca_path, ca_text = _material(remote.ca_bundle, f"{field}.ca_bundle", environment)
        try:
            context = ssl.create_default_context(cafile=ca_path, cadata=ca_text)
        except (ssl.SSLError, ValueError) as exc:
            raise ConfigurationError(f"{field}.ca_bundle could not be loaded") from exc
    else:
        context = httpx.create_ssl_context()
    context.set_alpn_protocols(["http/1.1"])
    if remote.client_certificate is None or remote.client_key is None:
        return context
    password = (
        _resolve(remote.client_key_password, f"{field}.client_key_password", environment)
        if remote.client_key_password
        else None
    )
    cert_path, cert_text = _material(
        remote.client_certificate, f"{field}.client_certificate", environment
    )
    key_path, key_text = _material(remote.client_key, f"{field}.client_key", environment)
    directory = tempfile.mkdtemp(prefix="algen-tls-") if cert_text or key_text else None
    try:
        if directory is not None:
            if cert_text is not None:
                cert_path = _private_file(directory, "certificate.pem", cert_text)
            if key_text is not None:
                key_path = _private_file(directory, "key.pem", key_text)
        try:
            context.load_cert_chain(cert_path or "", key_path, password)
        except (ssl.SSLError, OSError, ValueError):
            # The ssl error never names the key's contents, but keep the message fixed anyway.
            raise ConfigurationError(
                f"{field}: the client certificate or key could not be loaded"
            ) from None
    finally:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
    return context


def _private_file(directory: str, name: str, text: str) -> str:
    path = os.path.join(directory, name)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def build_outbound_network(
    settings: NetworkSettings, environment: Mapping[str, str] | None = None
) -> OutboundNetwork | None:
    """Resolve the network settings, or None when nothing is configured."""
    environment = environment or {}
    proxy: EgressProxy | None = None
    configured = settings.egress_proxy
    if configured is not None:
        url = (
            _resolve(configured.url, "network.egress_proxy.url", environment)
            if configured.url.startswith("env://")
            else configured.url
        )
        username = password = None
        parsed = urlparse(url)
        if parsed.username is not None or parsed.password is not None:
            # A URL from the environment may carry its credentials; move them out of it.
            username, password = unquote(parsed.username or ""), unquote(parsed.password or "")
            netloc = parsed.netloc.rsplit("@", 1)[1]
            url = parsed._replace(netloc=netloc).geturl()
        if configured.username is not None and configured.password is not None:
            username = _resolve(configured.username, "network.egress_proxy.username", environment)
            password = _resolve(configured.password, "network.egress_proxy.password", environment)
        proxy = EgressProxy(
            url=url,
            username=username,
            password=password,
            no_proxy=configured.no_proxy,
            resolve_targets=configured.resolve_targets,
        )
    remotes = tuple(
        RemoteIdentity(
            pattern=pattern.lower(),
            ssl_context=(
                _ssl_context(pattern, remote, environment)
                if remote.ca_bundle or remote.client_certificate
                else None
            ),
            pinned_sha256=frozenset(remote.pinned_sha256),
        )
        for pattern, remote in settings.remotes.items()
    )
    if proxy is None and not remotes:
        return None
    return OutboundNetwork(proxy=proxy, remotes=remotes)
