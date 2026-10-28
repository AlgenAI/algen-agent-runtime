"""OIDC/JWKS authentication, claim mapping, revocation, trusted-proxy identity and auth audits.

Keys are generated per test session; the identity provider is an ``httpx.MockTransport``.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

jwt = pytest.importorskip("jwt")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from algen_agent_runtime.api.app import create_app  # noqa: E402
from algen_agent_runtime.api.dependencies import principal_dependency  # noqa: E402
from algen_agent_runtime.auth import (  # noqa: E402
    AUTHENTICATION_AUDIT_TENANT,
    ApiAuthenticator,
    AuthenticationFailure,
    InMemoryTokenRevocationStore,
    JwksCache,
    JwksError,
    PostgresTokenRevocationStore,
    RevocationState,
)
from algen_agent_runtime.auth.jwks import KeysUnavailableError  # noqa: E402
from algen_agent_runtime.config.authentication import (  # noqa: E402
    OidcClaimMappingSettings,
    TrustedProxyIdentitySettings,
)
from algen_agent_runtime.config.settings import ApiSettings, AppSettings  # noqa: E402
from algen_agent_runtime.events.bus import InMemoryAuditLog  # noqa: E402
from algen_agent_runtime.exceptions.errors import ConfigurationError  # noqa: E402
from algen_agent_runtime.orchestration.container import build_container  # noqa: E402

ISSUER = "https://idp.example.com/realms/acme"
AUDIENCE = "algen-agent-runtime"
DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URL = "https://idp.example.com/realms/acme/protocol/openid-connect/certs"


def _rsa() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


RSA_KEY_1 = _rsa()
RSA_KEY_2 = _rsa()
EC_KEY = ec.generate_private_key(ec.SECP256R1())


def _jwk(private_key: Any, kid: str, alg: str | None = "RS256") -> dict[str, Any]:
    if isinstance(private_key, rsa.RSAPrivateKey):
        data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    else:
        data = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key()))
    data["kid"] = kid
    data["use"] = "sig"
    if alg is not None:
        data["alg"] = alg
    return data


class IdentityProvider:
    """A mock OIDC provider: discovery document, JWKS and a request log."""

    def __init__(self) -> None:
        self.keys: list[dict[str, Any]] = [_jwk(RSA_KEY_1, "key-1")]
        self.jwks_uri = JWKS_URL
        self.discovery_issuer = ISSUER
        self.fail_jwks = False
        self.requests: list[str] = []

    @property
    def jwks_fetches(self) -> int:
        return sum(1 for url in self.requests if url == self.jwks_uri)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url == DISCOVERY:
            return httpx.Response(
                200, json={"issuer": self.discovery_issuer, "jwks_uri": self.jwks_uri}
            )
        if url == self.jwks_uri:
            if self.fail_jwks:
                return httpx.Response(500)
            return httpx.Response(200, json={"keys": self.keys})
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _token(
    key: Any = RSA_KEY_1,
    *,
    kid: str | None = "key-1",
    alg: str = "RS256",
    lifetime: int = 300,
    issued_offset: int = 0,
    **claims: Any,
) -> str:
    now = int(time.time()) + issued_offset
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-1",
        "tenant_id": "tenant-1",
        "iat": now,
        "exp": now + lifetime,
        "scope": "runs:read runs:write",
        "jti": str(uuid.uuid4()),
    }
    payload.update(claims)
    payload = {name: value for name, value in payload.items() if value is not None}
    headers = {"kid": kid} if kid is not None else {}
    return str(jwt.encode(payload, key, algorithm=alg, headers=headers))


def _issuer(**overrides: Any) -> dict[str, Any]:
    return {"issuer": ISSUER, "audiences": [AUDIENCE], **overrides}


def _settings(
    *, issuer: dict[str, Any] | None = None, oidc: dict[str, Any] | None = None, **api: Any
) -> ApiSettings:
    return ApiSettings(
        auth_mode="oidc",
        oidc={"issuers": [issuer or _issuer()], **(oidc or {})},
        **api,
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _authenticator(
    provider: IdentityProvider,
    settings: ApiSettings | None = None,
    *,
    audits: InMemoryAuditLog | None = None,
    revocations: Any = None,
    clock: FakeClock | None = None,
) -> ApiAuthenticator:
    authenticator = ApiAuthenticator(
        settings or _settings(),
        audits=audits,
        revocations=revocations,
        oidc_transport=provider.transport,
    )
    if clock is not None:
        assert authenticator.oidc is not None
        for name in list(authenticator.oidc._issuers):
            authenticator.oidc.jwks(name)._clock = clock
    return authenticator


async def _reason(authenticator: ApiAuthenticator, token: str, **kwargs: Any) -> str:
    with pytest.raises(AuthenticationFailure) as caught:
        await authenticator.authenticate(authorization=f"Bearer {token}", **kwargs)
    return caught.value.reason


# --- OIDC discovery, JWKS caching and rotation -------------------------------------------------


async def test_oidc_discovers_jwks_and_maps_identity() -> None:
    provider = IdentityProvider()
    authenticator = _authenticator(provider)
    identity = await authenticator.authenticate(authorization=f"Bearer {_token()}")
    assert (identity.tenant_id, identity.user_id) == ("tenant-1", "user-1")
    assert identity.scopes == {"runs:read", "runs:write"}
    assert identity.method == "oidc" and identity.issuer == ISSUER and identity.key_id == "key-1"
    assert provider.requests == [DISCOVERY, JWKS_URL]
    # Cached: a second token does not refetch.
    await authenticator.authenticate(authorization=f"Bearer {_token()}")
    assert provider.jwks_fetches == 1


async def test_configured_jwks_url_skips_discovery() -> None:
    provider = IdentityProvider()
    authenticator = _authenticator(provider, _settings(issuer=_issuer(jwks_url=JWKS_URL)))
    await authenticator.authenticate(authorization=f"Bearer {_token()}")
    assert provider.requests == [JWKS_URL]


async def test_key_rotation_unknown_kid_refreshes_and_old_key_is_dropped() -> None:
    provider = IdentityProvider()
    clock = FakeClock()
    authenticator = _authenticator(provider, clock=clock)
    await authenticator.authenticate(authorization=f"Bearer {_token()}")
    provider.keys = [_jwk(RSA_KEY_2, "key-2")]
    clock.now += 31  # past the minimum refresh interval
    identity = await authenticator.authenticate(
        authorization=f"Bearer {_token(RSA_KEY_2, kid='key-2')}"
    )
    assert identity.key_id == "key-2"
    assert provider.jwks_fetches == 2
    clock.now += 31
    assert await _reason(authenticator, _token()) == "unknown_key"


async def test_unknown_kid_refresh_is_rate_limited() -> None:
    provider = IdentityProvider()
    clock = FakeClock()
    authenticator = _authenticator(provider, clock=clock)
    await authenticator.authenticate(authorization=f"Bearer {_token()}")
    for index in range(25):
        token = _token(kid=f"random-{index}")
        assert await _reason(authenticator, token) == "unknown_key"
    assert provider.jwks_fetches == 1
    clock.now += 30
    assert await _reason(authenticator, _token(kid="random-x")) == "unknown_key"
    assert provider.jwks_fetches == 2


async def test_failed_refresh_keeps_last_good_keys() -> None:
    provider = IdentityProvider()
    clock = FakeClock()
    authenticator = _authenticator(provider, clock=clock)
    await authenticator.authenticate(authorization=f"Bearer {_token()}")
    provider.fail_jwks = True
    clock.now += 301  # past the cache TTL: a refresh is attempted and fails
    identity = await authenticator.authenticate(authorization=f"Bearer {_token()}")
    assert identity.user_id == "user-1"
    assert provider.jwks_fetches == 2


async def test_unavailable_keys_fail_with_503() -> None:
    provider = IdentityProvider()
    provider.fail_jwks = True
    authenticator = _authenticator(provider)
    with pytest.raises(AuthenticationFailure) as caught:
        await authenticator.authenticate(authorization=f"Bearer {_token()}")
    assert caught.value.reason == "keys_unavailable"
    assert caught.value.status_code == 503


async def test_discovered_jwks_uri_must_be_on_an_allowed_host() -> None:
    provider = IdentityProvider()
    provider.jwks_uri = "https://keys.attacker.example/jwks"
    authenticator = _authenticator(provider)
    assert await _reason(authenticator, _token()) == "keys_unavailable"
    assert provider.requests == [DISCOVERY]  # the foreign jwks_uri was never fetched

    allowed = _authenticator(
        provider, _settings(issuer=_issuer(additional_allowed_hosts=["keys.attacker.example"]))
    )
    assert (await allowed.authenticate(authorization=f"Bearer {_token()}")).user_id == "user-1"


async def test_discovery_issuer_mismatch_is_refused() -> None:
    provider = IdentityProvider()
    provider.discovery_issuer = "https://other.example.com"
    assert await _reason(_authenticator(provider), _token()) == "keys_unavailable"


async def test_jwks_fetch_uses_ssrf_policy_for_private_addresses() -> None:
    # Without an injected transport the SSRF-safe transport resolves and refuses loopback.
    cache = JwksCache("https://127.0.0.1/issuer", jwks_url="https://127.0.0.1/jwks")
    with pytest.raises(JwksError, match="PolicyDeniedError"):
        await cache._fetch()
    with pytest.raises(KeysUnavailableError):
        await cache.get_key("key-1")
    assert cache.fetch_attempts == 1


async def test_symmetric_and_malformed_jwks_keys_are_ignored() -> None:
    provider = IdentityProvider()
    provider.keys = [
        {"kty": "oct", "kid": "hmac", "k": "c2VjcmV0"},
        {"kty": "RSA", "kid": "broken", "n": "!!", "e": "AQAB"},
        _jwk(RSA_KEY_1, "key-1"),
    ]
    authenticator = _authenticator(provider)
    await authenticator.authenticate(authorization=f"Bearer {_token()}")
    assert authenticator.oidc is not None
    assert authenticator.oidc.jwks(ISSUER).key_ids == {"key-1"}


# --- algorithm confusion and claim validation ---------------------------------------------------


async def test_hs256_token_signed_with_public_key_is_refused() -> None:
    provider = IdentityProvider()
    public_pem = (
        RSA_KEY_1.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    header = {"alg": "HS256", "kid": "key-1", "typ": "JWT"}
    payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "tenant_id": "t", "exp": 4e9}
    import base64
    import hashlib
    import hmac

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    signing_input = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(payload).encode())}"
    signature = hmac.new(public_pem.encode(), signing_input.encode(), hashlib.sha256).digest()
    forged = f"{signing_input}.{b64(signature)}"
    assert await _reason(_authenticator(provider), forged) == "algorithm_not_allowed"


async def test_none_algorithm_is_refused() -> None:
    provider = IdentityProvider()
    unsigned = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "tenant_id": "t", "exp": 4e9},
        None,
        algorithm="none",
        headers={"kid": "key-1"},
    )
    assert await _reason(_authenticator(provider), unsigned) == "algorithm_not_allowed"


async def test_algorithm_must_match_the_key_type() -> None:
    provider = IdentityProvider()
    settings = _settings(issuer=_issuer(algorithms=["RS256", "ES256"]))
    # An ES256 signature presented under the RSA key's kid.
    token = _token(EC_KEY, kid="key-1", alg="ES256")
    assert await _reason(_authenticator(provider, settings), token) == "algorithm_not_allowed"
    # An algorithm the issuer does not allow at all.
    assert await _reason(_authenticator(provider), token) == "algorithm_not_allowed"


async def test_ec_keys_verify_when_allowed() -> None:
    provider = IdentityProvider()
    provider.keys.append(_jwk(EC_KEY, "ec-1", alg="ES256"))
    settings = _settings(issuer=_issuer(algorithms=["RS256", "ES256"]))
    identity = await _authenticator(provider, settings).authenticate(
        authorization=f"Bearer {_token(EC_KEY, kid='ec-1', alg='ES256')}"
    )
    assert identity.key_id == "ec-1"


@pytest.mark.parametrize("algorithm", ["HS256", "none", "HS512"])
def test_oidc_settings_reject_symmetric_and_none_algorithms(algorithm: str) -> None:
    with pytest.raises(ValidationError, match="not allowed"):
        _settings(issuer=_issuer(algorithms=[algorithm]))


async def test_expiry_honours_bounded_clock_skew() -> None:
    provider = IdentityProvider()
    authenticator = _authenticator(provider, _settings(issuer=_issuer(leeway_seconds=60)))
    within_skew = _token(issued_offset=-330, lifetime=300)  # expired 30 s ago
    assert (await authenticator.authenticate(authorization=f"Bearer {within_skew}")).user_id
    beyond_skew = _token(issued_offset=-500, lifetime=300)  # expired 200 s ago
    assert await _reason(authenticator, beyond_skew) == "expired"
    future = _token(nbf=int(time.time()) + 600)
    assert await _reason(authenticator, future) == "not_yet_valid"


def test_clock_skew_is_bounded() -> None:
    with pytest.raises(ValidationError):
        _settings(issuer=_issuer(leeway_seconds=3600))


async def test_audience_issuer_signature_and_required_claims() -> None:
    provider = IdentityProvider()
    authenticator = _authenticator(provider)
    assert await _reason(authenticator, _token(aud="another-service")) == "invalid_audience"
    assert await _reason(authenticator, _token(iss="https://evil.example")) == "unknown_issuer"
    assert await _reason(authenticator, _token(exp=None)) == "missing_claim"
    tampered = _token(RSA_KEY_2)  # signed by a key that is not key-1
    assert await _reason(authenticator, tampered) == "invalid_signature"
    assert await _reason(authenticator, _token(kid=None)) == "missing_key_id"
    assert await _reason(authenticator, "not-a-jwt") == "malformed_token"
    required = _authenticator(provider, _settings(issuer=_issuer(required_claims=["azp"])))
    assert await _reason(required, _token()) == "missing_claim"


async def test_max_token_lifetime_enforces_short_lived_tokens() -> None:
    provider = IdentityProvider()
    authenticator = _authenticator(
        provider, _settings(issuer=_issuer(max_token_lifetime_seconds=600))
    )
    assert (await authenticator.authenticate(authorization=f"Bearer {_token()}")).user_id
    assert await _reason(authenticator, _token(lifetime=7200)) == "token_lifetime_exceeded"
    assert await _reason(authenticator, _token(iat=None)) == "missing_claim"


async def test_missing_bearer_token() -> None:
    authenticator = _authenticator(IdentityProvider())
    with pytest.raises(AuthenticationFailure) as caught:
        await authenticator.authenticate(authorization="Basic abc")
    assert caught.value.reason == "missing_credentials"


# --- claim mapping ------------------------------------------------------------------------------


async def test_claim_mapping_scopes_roles_groups_and_nested_claims() -> None:
    provider = IdentityProvider()
    claims = {
        "role_scopes": {"runtime-admin": ["*"], "analysts": ["conversations:read"]},
        "role_claims": ["realm_access.roles", "groups"],
        "tenant_claim": "https://example.com/tenant",
    }
    authenticator = _authenticator(provider, _settings(issuer=_issuer(claims=claims)))
    token = _token(
        scope=None,
        scp=["runs:read", "*"],
        realm_access={"roles": ["runtime-admin"]},
        groups=["analysts", "unmapped"],
        tenant_id=None,
        **{"https://example.com/tenant": "tenant-9"},
    )
    identity = await authenticator.authenticate(authorization=f"Bearer {token}")
    assert identity.tenant_id == "tenant-9"
    assert identity.scopes == {"runs:read", "*", "conversations:read"}

    # '*' in a token scope claim alone is never granted.
    plain = _authenticator(provider)
    identity = await plain.authenticate(authorization=f"Bearer {_token(scope='* runs:read')}")
    assert identity.scopes == {"runs:read"}


async def test_fixed_tenant_per_issuer_and_allowed_tenants() -> None:
    provider = IdentityProvider()
    fixed = _authenticator(
        provider, _settings(issuer=_issuer(claims={"fixed_tenant": "acme", "tenant_claim": None}))
    )
    identity = await fixed.authenticate(authorization=f"Bearer {_token(tenant_id='other')}")
    assert identity.tenant_id == "acme"

    restricted = _authenticator(
        provider, _settings(issuer=_issuer(claims={"allowed_tenants": ["tenant-1"]}))
    )
    assert await _reason(restricted, _token(tenant_id="tenant-2")) == "tenant_not_allowed"


async def test_missing_tenant_fails_closed() -> None:
    provider = IdentityProvider()
    authenticator = _authenticator(provider)
    assert await _reason(authenticator, _token(tenant_id=None)) == "missing_tenant"
    assert await _reason(authenticator, _token(tenant_id="  ")) == "missing_tenant"
    assert await _reason(authenticator, _token(tenant_id=["a"])) == "missing_tenant"
    assert await _reason(authenticator, _token(sub="")) == "missing_subject"


def test_claim_mapping_needs_a_tenant_source() -> None:
    with pytest.raises(ValidationError, match="tenant"):
        OidcClaimMappingSettings(tenant_claim=None)


# --- revocation ---------------------------------------------------------------------------------


def _revocation_settings(**revocation: Any) -> ApiSettings:
    return _settings(token_revocation={"enabled": True, **revocation})


async def test_revoked_jti_is_refused_per_tenant() -> None:
    provider = IdentityProvider()
    store = InMemoryTokenRevocationStore()
    authenticator = _authenticator(provider, _revocation_settings(), revocations=store)
    expires = datetime.now(UTC) + timedelta(minutes=5)
    await store.revoke_token("tenant-1", "jti-1", expires)
    assert await _reason(authenticator, _token(jti="jti-1")) == "revoked"
    assert (await authenticator.authenticate(authorization=f"Bearer {_token(jti='jti-2')}")).user_id
    # The same jti in another tenant is not affected.
    await authenticator.authenticate(authorization=f"Bearer {_token(jti='jti-1', tenant_id='t2')}")
    # Already-expired entries are not kept.
    await store.revoke_token("tenant-1", "old", datetime.now(UTC) - timedelta(seconds=1))
    assert await store.state("tenant-1", "old", "user-1") == RevocationState()


async def test_subject_revoked_before_refuses_older_tokens_only() -> None:
    provider = IdentityProvider()
    store = InMemoryTokenRevocationStore()
    authenticator = _authenticator(provider, _revocation_settings(), revocations=store)
    await store.revoke_subject("tenant-1", "user-1", datetime.now(UTC) - timedelta(seconds=60))
    assert await _reason(authenticator, _token(issued_offset=-120)) == "revoked"
    assert await _reason(authenticator, _token(iat=None)) == "revoked"
    fresh = _token(issued_offset=5)
    assert (await authenticator.authenticate(authorization=f"Bearer {fresh}")).user_id
    # Monotonic: an earlier cutoff does not un-revoke.
    await store.revoke_subject("tenant-1", "user-1", datetime.now(UTC) - timedelta(days=1))
    assert await _reason(authenticator, _token(issued_offset=-120)) == "revoked"


async def test_require_jti_and_store_outage_fail_closed() -> None:
    provider = IdentityProvider()
    authenticator = _authenticator(
        provider,
        _revocation_settings(require_jti=True),
        revocations=InMemoryTokenRevocationStore(),
    )
    assert await _reason(authenticator, _token(jti=None)) == "missing_jti"

    class BrokenStore(InMemoryTokenRevocationStore):
        async def state(self, tenant_id: str, jti: str | None, subject: str) -> RevocationState:
            raise ConnectionError("database down")

    broken = _authenticator(provider, _revocation_settings(), revocations=BrokenStore())
    with pytest.raises(AuthenticationFailure) as caught:
        await broken.authenticate(authorization=f"Bearer {_token()}")
    assert caught.value.reason == "revocation_unavailable"
    assert caught.value.status_code == 503


def test_revocation_enabled_without_a_store_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError, match="revocation"):
        ApiAuthenticator(_revocation_settings(), oidc_transport=IdentityProvider().transport)


class FakeDatabase:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *arguments: Any) -> str:
        self.calls.append((query, arguments))
        return "DELETE 3"

    async def fetchrow(self, query: str, *arguments: Any) -> Any:
        self.calls.append((query, arguments))
        return self.row


async def test_postgres_revocation_store_queries() -> None:
    cutoff = datetime.now(UTC)
    database = FakeDatabase({"token_revoked": True, "subject_revoked_before": cutoff})
    store = PostgresTokenRevocationStore(database)
    state = await store.state("tenant-1", "jti-1", "user-1")
    assert state == RevocationState(token_revoked=True, subject_revoked_before=cutoff)
    query, arguments = database.calls[-1]
    assert "algen_agent_runtime_revoked_tokens" in query and arguments[:3] == (
        "tenant-1",
        "jti-1",
        "user-1",
    )
    # A token without a jti is never matched by the jti denylist.
    assert not (await store.state("tenant-1", None, "user-1")).token_revoked
    await store.revoke_token("tenant-1", "jti-2", cutoff + timedelta(minutes=5), reason="leak")
    assert "ON CONFLICT (tenant_id, jti)" in database.calls[-1][0]
    assert await store.purge_expired() == 3


def test_postgres_revocation_requires_dsn_and_jwt_or_oidc() -> None:
    with pytest.raises(ValidationError, match="postgres_dsn"):
        AppSettings(
            api=_settings(token_revocation={"enabled": True, "store": "postgres"}).model_dump()
        )
    with pytest.raises(ValidationError, match="require jwt or oidc"):
        ApiSettings(token_revocation={"enabled": True})


# --- trusted-proxy service identity -------------------------------------------------------------


def _proxy_settings(**overrides: Any) -> ApiSettings:
    proxy = {
        "enabled": True,
        "trusted_proxies": ["10.0.0.0/24"],
        "identities": {
            "spiffe://acme/ns/jobs/sa/scheduler": {
                "tenant_id": "tenant-1",
                "scopes": ["runs:write"],
            }
        },
        **overrides,
    }
    return _settings(trusted_proxy_identity=proxy)


def _request(
    headers: dict[str, str], client: tuple[str, int] | None = ("10.0.0.7", 5000)
) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
            "client": client,
        }
    )


SERVICE = "spiffe://acme/ns/jobs/sa/scheduler"


async def test_trusted_proxy_identity_maps_to_service_principal() -> None:
    provider = IdentityProvider()
    audits = InMemoryAuditLog()
    dependency = principal_dependency(
        _proxy_settings(), audits=audits, oidc_transport=provider.transport
    )
    identity = await dependency(_request({"x-client-cert-identity": SERVICE}))
    assert (identity.tenant_id, identity.user_id) == ("tenant-1", SERVICE)
    assert identity.scopes == {"runs:write"}
    assert provider.requests == []  # no bearer verification needed
    # Without the header the bearer token path still works through the proxy.
    user = await dependency(_request({"authorization": f"Bearer {_token()}"}))
    assert user.user_id == "user-1"


async def test_spoofed_identity_header_from_untrusted_peer_is_rejected() -> None:
    provider = IdentityProvider()
    audits = InMemoryAuditLog()
    dependency = principal_dependency(
        _proxy_settings(), audits=audits, oidc_transport=provider.transport
    )
    spoofed = _request(
        {
            "x-client-cert-identity": SERVICE,
            "x-forwarded-for": "10.0.0.7",
            "authorization": f"Bearer {_token()}",
        },
        client=("203.0.113.9", 4000),
    )
    with pytest.raises(HTTPException) as caught:
        await dependency(spoofed)
    assert caught.value.status_code == 401
    records = await audits.list(AUTHENTICATION_AUDIT_TENANT)
    assert [record.metadata["reason"] for record in records] == ["untrusted_proxy"]
    for client in (None, ("::ffff:203.0.113.9", 1), ("not-an-ip", 1)):
        with pytest.raises(HTTPException):
            await dependency(_request({"x-client-cert-identity": SERVICE}, client=client))
    # An IPv4-mapped trusted address is accepted.
    mapped = _request({"x-client-cert-identity": SERVICE}, client=("::ffff:10.0.0.7", 1))
    assert (await dependency(mapped)).user_id == SERVICE


async def test_unknown_or_duplicated_service_identity_is_rejected() -> None:
    dependency = principal_dependency(
        _proxy_settings(), oidc_transport=IdentityProvider().transport
    )
    with pytest.raises(HTTPException):
        await dependency(_request({"x-client-cert-identity": "spiffe://acme/other"}))
    duplicated = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [
                (b"x-client-cert-identity", SERVICE.encode()),
                (b"x-client-cert-identity", b"spiffe://acme/other"),
            ],
            "client": ("10.0.0.7", 1),
        }
    )
    with pytest.raises(HTTPException):
        await dependency(duplicated)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"trusted_proxies": ["0.0.0.0/0"]}, "every address"),
        ({"trusted_proxies": []}, "requires trusted_proxies"),
        ({"identities": {}}, "requires identities"),
        ({"header": "Authorization"}, "may not be"),
        ({"header": "x-tenant-id"}, "may not be"),
        ({"header": "bad header"}, "valid header"),
    ],
)
def test_trusted_proxy_settings_are_narrow(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _proxy_settings(**overrides)


def test_trusted_proxy_is_off_by_default_and_needs_verified_auth() -> None:
    assert TrustedProxyIdentitySettings().enabled is False
    with pytest.raises(ValidationError, match="require jwt or oidc"):
        ApiSettings(
            trusted_proxy_identity={
                "enabled": True,
                "trusted_proxies": ["10.0.0.0/8"],
                "identities": {"svc": {"tenant_id": "t"}},
            }
        )


# --- audit records ------------------------------------------------------------------------------


async def test_failure_audits_are_content_minimal_and_never_hold_the_token() -> None:
    provider = IdentityProvider()
    audits = InMemoryAuditLog()
    dependency = principal_dependency(
        _revocation_settings(),
        audits=audits,
        oidc_transport=provider.transport,
        revocations=(store := InMemoryTokenRevocationStore()),
    )
    tokens = {
        "expired": _token(issued_offset=-1000),
        "audience": _token(aud="other"),
        "signature": _token(RSA_KEY_2),
        "unknown_key": _token(kid="nope"),
        "tenant": _token(tenant_id=None),
        "revoked": _token(jti="burned"),
    }
    await store.revoke_token("tenant-1", "burned", datetime.now(UTC) + timedelta(minutes=5))
    for token in tokens.values():
        with pytest.raises(HTTPException) as caught:
            await dependency(_request({"authorization": f"Bearer {token}"}))
        assert caught.value.headers == {"WWW-Authenticate": 'Bearer error="invalid_token"'}
        assert token not in str(caught.value.detail)

    unauthenticated = await audits.list(AUTHENTICATION_AUDIT_TENANT)
    tenant = await audits.list("tenant-1")
    reasons = [record.metadata["reason"] for record in (*unauthenticated, *tenant)]
    assert sorted(reasons) == sorted(
        [
            "expired",
            "invalid_audience",
            "invalid_signature",
            "unknown_key",
            "missing_tenant",
            "revoked",
        ]
    )
    for record in (*unauthenticated, *tenant):
        assert record.action == "auth.authenticate" and record.outcome == "failure"
        assert set(record.metadata) <= {"method", "reason", "issuer", "kid", "subject"}
        serialized = record.model_dump_json()
        assert all(token not in serialized for token in tokens.values())
        assert all(part not in serialized for token in tokens.values() for part in token.split("."))
    revoked = next(record for record in tenant if record.metadata["reason"] == "revoked")
    assert revoked.metadata == {
        "method": "oidc",
        "reason": "revoked",
        "issuer": ISSUER,
        "kid": "key-1",
        "subject": "user-1",
    }
    # Unverified failures carry no subject.
    assert all(
        "subject" not in record.metadata
        for record in unauthenticated
        if record.metadata["reason"] != "missing_tenant"
    )


async def test_success_audits_are_opt_in() -> None:
    provider = IdentityProvider()
    audits = InMemoryAuditLog()
    quiet = principal_dependency(_settings(), audits=audits, oidc_transport=provider.transport)
    await quiet(_request({"authorization": f"Bearer {_token()}"}))
    assert await audits.list("tenant-1") == ()

    loud = principal_dependency(
        _settings(authentication_audit={"record_success": True}),
        audits=audits,
        oidc_transport=provider.transport,
    )
    await loud(_request({"authorization": f"Bearer {_token()}"}))
    (record,) = await audits.list("tenant-1")
    assert record.outcome == "success" and record.actor_id == "user-1"
    assert record.metadata == {
        "method": "oidc",
        "issuer": ISSUER,
        "kid": "key-1",
        "subject": "user-1",
    }


async def test_failure_audit_flood_is_capped_and_summarized() -> None:
    provider = IdentityProvider()
    audits = InMemoryAuditLog()
    clock = FakeClock()
    authenticator = _authenticator(
        provider,
        _settings(authentication_audit={"max_failure_records_per_minute": 3}),
        audits=audits,
    )
    authenticator.auditor._clock = clock
    authenticator.auditor._window_start = clock.now
    for _ in range(10):
        await _reason(authenticator, "garbage")
    assert len(await audits.list(AUTHENTICATION_AUDIT_TENANT)) == 3
    clock.now += 61
    await _reason(authenticator, "garbage")
    records = await audits.list(AUTHENTICATION_AUDIT_TENANT)
    summary = [record for record in records if record.action == "auth.audit.suppressed"]
    assert summary[0].metadata == {"count": 7}
    assert len(records) == 5


# --- static JWT mode and settings ---------------------------------------------------------------


async def test_static_jwt_mode_is_unchanged_and_audits_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_pem = RSA_KEY_1.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    monkeypatch.setenv("TEST_STATIC_JWT_KEY", public_pem.decode())
    audits = InMemoryAuditLog()
    settings = ApiSettings(
        auth_mode="jwt",
        jwt_key="env://TEST_STATIC_JWT_KEY",
        jwt_issuer=ISSUER,
        jwt_audience=AUDIENCE,
    )
    dependency = principal_dependency(settings, audits=audits)
    identity = await dependency(_request({"authorization": f"Bearer {_token()}"}))
    assert identity.scopes == {"runs:read", "runs:write"}
    with pytest.raises(HTTPException) as caught:
        await dependency(_request({"authorization": f"Bearer {_token(tenant_id=None)}"}))
    assert caught.value.detail == "invalid bearer token"  # tenant_id is a required claim
    with pytest.raises(HTTPException) as caught:
        await dependency(_request({}))
    assert caught.value.detail == "bearer token is required"
    reasons = [
        record.metadata["reason"] for record in await audits.list(AUTHENTICATION_AUDIT_TENANT)
    ]
    assert reasons == ["missing_claim", "missing_credentials"]


def test_jwt_algorithms_may_not_include_none() -> None:
    with pytest.raises(ValidationError, match="none"):
        ApiSettings(auth_mode="jwt", jwt_key="env://K", jwt_algorithms=["none"])


def test_oidc_settings_validation() -> None:
    with pytest.raises(ValidationError, match="at least one oidc issuer"):
        ApiSettings(auth_mode="oidc")
    with pytest.raises(ValidationError, match="https"):
        _settings(issuer=_issuer(issuer="http://idp.local"))
    assert _settings(issuer=_issuer(issuer="http://idp.local"), oidc={"allow_insecure_http": True})
    with pytest.raises(ValidationError, match="unique"):
        ApiSettings(auth_mode="oidc", oidc={"issuers": [_issuer(), _issuer()]})
    with pytest.raises(ValidationError):
        _settings(issuer={"issuer": ISSUER, "audiences": []})
    with pytest.raises(ValidationError, match="Extra inputs"):
        _settings(issuer=_issuer(unknown_field=True))


def test_development_headers_stay_loopback_only() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        ApiSettings(host="0.0.0.0")


def test_missing_auth_extra_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "jwt", None)
    with pytest.raises(ConfigurationError, match=r"algen-agent-runtime\[auth\]"):
        principal_dependency(_settings())


# --- through the HTTP app -----------------------------------------------------------------------


def _app(settings: AppSettings, provider: IdentityProvider) -> tuple[TestClient, Any]:
    container = dataclasses.replace(build_container(settings), oidc_transport=provider.transport)
    return TestClient(create_app(settings, container)), container


def test_revocation_routes_and_single_audit_per_request() -> None:
    provider = IdentityProvider()
    settings = AppSettings(
        api=_settings(
            token_revocation={"enabled": True},
            rate_limiting={"enabled": True},
        ).model_dump()
    )
    client, container = _app(settings, provider)
    admin = _token(scope="auth:revoke runs:read")
    victim_jti = "victim-jti"
    victim = _token(jti=victim_jti)
    with client:
        assert (
            client.get(
                "/v1/runs/missing", headers={"Authorization": f"Bearer {victim}"}
            ).status_code
            == 404
        )
        response = client.post(
            "/v1/auth/revocations/tokens",
            headers={"Authorization": f"Bearer {admin}"},
            json={
                "jti": victim_jti,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                "reason": "leaked",
            },
        )
        assert response.status_code == 200, response.text
        denied = client.get("/v1/runs/missing", headers={"Authorization": f"Bearer {victim}"})
        assert denied.status_code == 401
        assert denied.headers["www-authenticate"] == 'Bearer error="invalid_token"'

        # Subject revocation needs the auth:revoke scope and a cutoff that is not in the future.
        reader = _token(scope="runs:read")
        assert (
            client.post(
                "/v1/auth/revocations/subjects",
                headers={"Authorization": f"Bearer {reader}"},
                json={"subject": "user-2"},
            ).status_code
            == 403
        )
        future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        assert (
            client.post(
                "/v1/auth/revocations/subjects",
                headers={"Authorization": f"Bearer {admin}"},
                json={"subject": "user-2", "revoked_before": future},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/auth/revocations/subjects",
                headers={"Authorization": f"Bearer {admin}"},
                json={"subject": "user-2"},
            ).status_code
            == 200
        )
        user_2 = _token(sub="user-2", issued_offset=-5)
        assert (
            client.get(
                "/v1/runs/missing", headers={"Authorization": f"Bearer {user_2}"}
            ).status_code
            == 401
        )

    async def records() -> tuple[Any, ...]:
        return await container.runtime.audits.list("tenant-1")

    import asyncio

    tenant_records = asyncio.run(records())
    actions = [record.action for record in tenant_records]
    assert actions.count("auth.token.revoked") == 1
    assert actions.count("auth.subject.revoked") == 1
    revoked_failures = [
        record
        for record in tenant_records
        if record.action == "auth.authenticate" and record.metadata.get("reason") == "revoked"
    ]
    # Rate-limit middleware and route both authenticate, but each request is audited once.
    assert len(revoked_failures) == 2


def test_revocation_routes_return_404_when_disabled() -> None:
    provider = IdentityProvider()
    client, _ = _app(AppSettings(api=_settings().model_dump()), provider)
    with client:
        response = client.post(
            "/v1/auth/revocations/subjects",
            headers={"Authorization": f"Bearer {_token(scope='auth:revoke')}"},
            json={"subject": "user-2"},
        )
    assert response.status_code == 404


def test_trusted_proxy_through_the_app() -> None:
    provider = IdentityProvider()
    settings = AppSettings(api=_proxy_settings().model_dump())
    container = dataclasses.replace(build_container(settings), oidc_transport=provider.transport)
    app = create_app(settings, container)
    trusted = TestClient(app, client=("10.0.0.7", 5000))
    untrusted = TestClient(app, client=("198.51.100.4", 5000))
    body = {"agent": "missing", "input": "hi"}
    with trusted:
        ok = trusted.post("/v1/runs", headers={"X-Client-Cert-Identity": SERVICE}, json=body)
        assert ok.status_code != 401
    with untrusted:
        spoofed = untrusted.post(
            "/v1/runs",
            headers={"X-Client-Cert-Identity": SERVICE, "X-Forwarded-For": "10.0.0.7"},
            json=body,
        )
        assert spoofed.status_code == 401
