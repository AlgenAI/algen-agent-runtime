"""JWKS discovery, caching and rate-limited refresh for one OIDC issuer.

Keys are fetched through the SSRF-safe HTTP client: only the issuer host, the configured
``jwks_url`` host and explicitly listed extra hosts are reachable, private networks are refused
unless configured, redirects are not followed and responses are size-capped. Fetched keys are
used for ``ttl_seconds``. A token naming an unknown ``kid`` triggers a refresh, but never more
often than ``min_refresh_interval_seconds``, so random key ids cannot turn every request into an
upstream fetch. When a refresh fails the last good keys stay in use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from algen_agent_runtime.exceptions.errors import PolicyDeniedError
from algen_agent_runtime.security.network import (
    OutboundNetwork,
    check_host_allowlist,
    create_safe_http_client,
    normalize_and_validate_url,
)

logger = logging.getLogger(__name__)

_MAX_KEYS = 100
_KEY_TYPES = {"RSA", "EC", "OKP"}


class JwksError(Exception):
    """Discovery or JWKS fetch failed (network, policy, status, size or format)."""


class UnknownKeyError(Exception):
    """No key with the requested id is published, even after an allowed refresh."""


class KeysUnavailableError(Exception):
    """No key set has ever been fetched for this issuer."""


@dataclass(frozen=True)
class JsonWebKey:
    """A verification key from a JWKS, with the metadata needed for algorithm checks."""

    kid: str
    key_type: str
    algorithm: str | None
    key: Any


def algorithm_matches_key(algorithm: str, key: JsonWebKey) -> bool:
    """Whether a JWS algorithm may be used with this key (blocks cross-family confusion)."""
    if key.algorithm is not None and key.algorithm != algorithm:
        return False
    if key.key_type == "RSA":
        return algorithm[:2] in {"RS", "PS"}
    if key.key_type == "EC":
        return algorithm.startswith("ES")
    if key.key_type == "OKP":
        return algorithm == "EdDSA"
    return False


class JwksCache:
    """Keys of one issuer, discovered and refreshed under the network and rate policy."""

    def __init__(
        self,
        issuer: str,
        *,
        jwks_url: str | None = None,
        additional_allowed_hosts: tuple[str, ...] = (),
        allow_private_networks: bool = False,
        allow_insecure_http: bool = False,
        ttl_seconds: float = 300,
        min_refresh_interval_seconds: float = 30,
        timeout_seconds: float = 5,
        max_bytes: int = 262_144,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        outbound: OutboundNetwork | None = None,
    ) -> None:
        self.issuer = issuer
        self._configured_jwks_url = jwks_url
        self._jwks_url = jwks_url
        hosts = [urlparse(issuer).hostname or ""]
        if jwks_url is not None:
            hosts.append(urlparse(jwks_url).hostname or "")
        hosts.extend(additional_allowed_hosts)
        # Never empty: an empty allowlist would mean "any host" to check_host_allowlist.
        self._allowed_hosts = tuple(host for host in hosts if host)
        self._allow_private_networks = allow_private_networks
        self._allow_insecure_http = allow_insecure_http
        self._ttl = ttl_seconds
        self._min_interval = min_refresh_interval_seconds
        self._timeout = timeout_seconds
        self._max_bytes = max_bytes
        self._transport = transport
        self._outbound = outbound
        self._clock = clock
        self._keys: dict[str, JsonWebKey] = {}
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None
        self._lock = asyncio.Lock()
        #: Number of JWKS fetch attempts (successful or not); useful for tests and diagnostics.
        self.fetch_attempts = 0

    @property
    def key_ids(self) -> frozenset[str]:
        return frozenset(self._keys)

    async def get_key(self, kid: str) -> JsonWebKey:
        now = self._clock()
        stale = self._fetched_at is None or now - self._fetched_at >= self._ttl
        if stale or kid not in self._keys:
            await self._refresh()
        key = self._keys.get(kid)
        if key is not None:
            return key
        if self._fetched_at is None:
            raise KeysUnavailableError(self.issuer)
        raise UnknownKeyError(kid)

    async def _refresh(self) -> None:
        async with self._lock:
            now = self._clock()
            if self._last_attempt is not None and now - self._last_attempt < self._min_interval:
                return
            self._last_attempt = now
            self.fetch_attempts += 1
            try:
                keys = await self._fetch()
            except JwksError as exc:
                logger.warning(
                    "JWKS refresh failed; keeping the last good keys",
                    extra={"issuer": self.issuer, "error": str(exc)},
                )
                return
            self._keys = keys
            self._fetched_at = now

    def _check_url(self, url: str) -> None:
        if urlparse(url).scheme != "https" and not self._allow_insecure_http:
            raise JwksError(f"{url!r} must use https")
        try:
            host, _ = normalize_and_validate_url(url)
            check_host_allowlist(host, self._allowed_hosts)
        except PolicyDeniedError as exc:
            raise JwksError(str(exc)) from exc

    async def _get_json(self, client: httpx.AsyncClient, url: str) -> Any:
        self._check_url(url)
        try:
            async with client.stream(
                "GET", url, headers={"Accept": "application/json"}
            ) as response:
                if response.status_code != 200:
                    raise JwksError(f"{url!r} returned HTTP {response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_bytes:
                        raise JwksError(f"{url!r} response exceeds {self._max_bytes} bytes")
        except (httpx.HTTPError, PolicyDeniedError) as exc:
            raise JwksError(f"{url!r} could not be fetched: {type(exc).__name__}") from exc
        try:
            return json.loads(bytes(body))
        except ValueError as exc:
            raise JwksError(f"{url!r} did not return JSON") from exc

    async def _discover(self, client: httpx.AsyncClient) -> str:
        url = self.issuer.rstrip("/") + "/.well-known/openid-configuration"
        document = await self._get_json(client, url)
        if not isinstance(document, dict):
            raise JwksError("discovery document is not an object")
        if document.get("issuer") != self.issuer:
            raise JwksError("discovery document issuer does not match the configured issuer")
        jwks_uri = document.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri:
            raise JwksError("discovery document has no jwks_uri")
        return jwks_uri

    async def _fetch(self) -> dict[str, JsonWebKey]:
        async with create_safe_http_client(
            allowed_hosts=self._allowed_hosts,
            allow_private_networks=self._allow_private_networks,
            timeout=self._timeout,
            follow_redirects=False,
            transport=self._transport,
            outbound=self._outbound,
        ) as client:
            jwks_url = self._jwks_url
            if jwks_url is None:
                jwks_url = await self._discover(client)
            document = await self._get_json(client, jwks_url)
            keys = _parse_jwks(document)
            # Remember a discovered URL only once it served a usable key set.
            self._jwks_url = jwks_url
            return keys


def _parse_jwks(document: Any) -> dict[str, JsonWebKey]:
    import jwt

    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise JwksError("JWKS document has no keys list")
    entries = document["keys"]
    if len(entries) > _MAX_KEYS:
        raise JwksError(f"JWKS document has more than {_MAX_KEYS} keys")
    keys: dict[str, JsonWebKey] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kid = entry.get("kid")
        key_type = entry.get("kty")
        algorithm = entry.get("alg")
        # Symmetric ("oct") keys never belong in a published JWKS; ignore them outright.
        if not isinstance(kid, str) or not kid or key_type not in _KEY_TYPES:
            continue
        if entry.get("use") not in (None, "sig"):
            continue
        if algorithm is not None and not isinstance(algorithm, str):
            continue
        try:
            parsed = jwt.PyJWK(entry)
        except Exception:
            logger.warning("ignoring a malformed JWKS key", extra={"kid": kid[:256]})
            continue
        keys[kid] = JsonWebKey(kid=kid, key_type=key_type, algorithm=algorithm, key=parsed.key)
    if not keys:
        raise JwksError("JWKS document has no usable signing keys")
    return keys
