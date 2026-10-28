"""Settings for verified API authentication.

These models configure the ``oidc`` authentication mode (issuers, JWKS discovery and caching,
claim-to-principal mapping), the optional token revocation check, the trusted-proxy service
identity hook and authentication audit records. They live under ``api`` in ``AppSettings``.
See docs/operations.md#api-authentication.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Asymmetric JWS algorithms accepted for keys published in a JWKS. ``none`` and the HMAC
#: (``HS*``) algorithms are never accepted in ``oidc`` mode: a JWKS publishes public keys, and an
#: HMAC "verified" with a public key is the classic algorithm-confusion forgery.
ASYMMETRIC_JWT_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
        "EdDSA",
    }
)

# Headers that already carry identity or credentials; never usable as the forwarded identity.
_RESERVED_IDENTITY_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "host",
        "x-tenant-id",
        "x-user-id",
        "x-scopes",
        "x-forwarded-for",
        "forwarded",
    }
)
_HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{0,127}$")


class _AuthSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _validate_endpoint(url: str, field: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError(f"{field} must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field} must not contain a query or fragment")


class OidcClaimMappingSettings(_AuthSettings):
    """How verified token claims become the Runtime principal (tenant, user and scopes).

    A claim name is first looked up as a literal key (namespaced claims such as
    ``https://example.com/tenant`` contain dots), then as a dotted path into nested objects
    (``realm_access.roles``).
    """

    subject_claim: str = Field(
        default="sub", min_length=1, description="Claim that becomes the principal's user id."
    )
    tenant_claim: str | None = Field(
        default="tenant_id",
        description="Claim that carries the tenant id. Ignored when fixed_tenant is set.",
    )
    fixed_tenant: str | None = Field(
        default=None,
        min_length=1,
        description="Tenant for every token from this issuer, for one-tenant identity providers. "
        "Takes precedence over tenant_claim.",
    )
    allowed_tenants: tuple[str, ...] = Field(
        default=(),
        description="When set, a token whose tenant is not listed is rejected.",
    )
    scope_claims: tuple[str, ...] = Field(
        default=("scope", "scp"),
        description="Claims whose values (a space-separated string or a list) are granted as "
        "Runtime scopes. The wildcard '*' is never granted from a token claim; grant it through "
        "role_scopes if it is really needed.",
    )
    role_claims: tuple[str, ...] = Field(
        default=("roles", "groups"),
        description="Claims holding roles or groups that role_scopes maps to Runtime scopes.",
    )
    role_scopes: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        description="Role or group value -> Runtime scopes it grants, e.g. "
        "{'runtime-operators': ['runs:read', 'runs:write']}.",
    )

    @model_validator(mode="after")
    def validate_tenant_source(self) -> OidcClaimMappingSettings:
        if not self.fixed_tenant and not self.tenant_claim:
            raise ValueError("claim mapping needs tenant_claim or fixed_tenant")
        return self


class OidcIssuerSettings(_AuthSettings):
    """One trusted OpenID Connect issuer and the tokens it may mint for the Runtime."""

    issuer: str = Field(
        description="Exact issuer identifier; tokens must carry it as 'iss'. Its host is "
        "allowlisted for discovery and JWKS fetches."
    )
    jwks_url: str | None = Field(
        default=None,
        description="JWKS endpoint. When omitted it is discovered from "
        "{issuer}/.well-known/openid-configuration.",
    )
    audiences: tuple[str, ...] = Field(
        min_length=1, description="Accepted 'aud' values; a token must name at least one."
    )
    algorithms: tuple[str, ...] = Field(
        default=("RS256",),
        min_length=1,
        description="Accepted asymmetric JWS algorithms (RS*, PS*, ES*, EdDSA).",
    )
    leeway_seconds: int = Field(
        default=30, ge=0, le=300, description="Clock skew allowed for exp, nbf and iat."
    )
    max_token_lifetime_seconds: int | None = Field(
        default=None,
        ge=60,
        le=86_400,
        description="When set, tokens must carry iat and exp - iat may not exceed this. "
        "Enforces short-lived tokens, the primary revocation control.",
    )
    required_claims: tuple[str, ...] = Field(
        default=(),
        description="Claims that must be present in addition to exp, iss, aud and the subject.",
    )
    additional_allowed_hosts: tuple[str, ...] = Field(
        default=(),
        description="Extra hosts a discovered jwks_uri may live on (the issuer host and the "
        "jwks_url host are always allowed).",
    )
    claims: OidcClaimMappingSettings = Field(default_factory=OidcClaimMappingSettings)

    @model_validator(mode="after")
    def validate_issuer(self) -> OidcIssuerSettings:
        _validate_endpoint(self.issuer, "oidc issuer")
        if self.jwks_url is not None:
            _validate_endpoint(self.jwks_url, "oidc jwks_url")
        for algorithm in self.algorithms:
            if algorithm not in ASYMMETRIC_JWT_ALGORITHMS:
                raise ValueError(
                    f"oidc algorithm {algorithm!r} is not allowed; use asymmetric algorithms "
                    f"({', '.join(sorted(ASYMMETRIC_JWT_ALGORITHMS))})"
                )
        return self


class OidcSettings(_AuthSettings):
    """``auth_mode: oidc``: bearer JWTs verified against issuer-published JWKS keys."""

    issuers: tuple[OidcIssuerSettings, ...] = Field(
        default=(), description="Trusted issuers; a token is routed to one by its 'iss'."
    )
    jwks_cache_ttl_seconds: int = Field(
        default=300, ge=30, le=86_400, description="How long fetched keys are used before refresh."
    )
    jwks_min_refresh_interval_seconds: float = Field(
        default=30,
        ge=1,
        le=3_600,
        description="Minimum time between JWKS fetches per issuer, including refreshes "
        "triggered by an unknown key id, so random 'kid' values cannot flood the issuer.",
    )
    http_timeout_seconds: float = Field(default=5, gt=0, le=60)
    max_jwks_bytes: int = Field(default=262_144, ge=1_024, le=4_194_304)
    allow_private_networks: bool = Field(
        default=False,
        description="Allow discovery/JWKS fetches to private, loopback or link-local "
        "addresses (an in-cluster identity provider).",
    )
    allow_insecure_http: bool = Field(
        default=False,
        description="Allow http:// issuer and JWKS URLs. Local test identity providers only.",
    )

    @model_validator(mode="after")
    def validate_issuers(self) -> OidcSettings:
        names = [item.issuer for item in self.issuers]
        if len(names) != len(set(names)):
            raise ValueError("oidc issuers must be unique")
        if not self.allow_insecure_http:
            for item in self.issuers:
                for url in (item.issuer, item.jwks_url):
                    if url is not None and urlparse(url).scheme != "https":
                        raise ValueError(
                            f"oidc URL {url!r} must use https (allow_insecure_http is false)"
                        )
        return self


class TokenRevocationSettings(_AuthSettings):
    """Optional check of verified tokens against a revocation store."""

    enabled: bool = Field(
        default=False,
        description="Check each verified bearer token's jti and subject against the store.",
    )
    store: Literal["memory", "postgres"] = Field(
        default="memory",
        description="Where revocations live. 'memory' is per-process and lost on restart.",
    )
    require_jti: bool = Field(
        default=False, description="Reject tokens without a jti claim when revocation is on."
    )


class ServicePrincipalSettings(_AuthSettings):
    """The principal a forwarded service identity is mapped to."""

    tenant_id: str = Field(min_length=1)
    user_id: str | None = Field(
        default=None, description="Principal user id; defaults to the identity string."
    )
    scopes: tuple[str, ...] = ()


class TrustedProxyIdentitySettings(_AuthSettings):
    """Accept a client-certificate identity forwarded by a TLS-terminating proxy.

    Off by default. The header is honoured only when the direct TCP peer is inside
    ``trusted_proxies``; the same header from any other address is rejected and audited. The
    proxy must overwrite (never append to) the header on every request.
    """

    enabled: bool = False
    header: str = Field(
        default="x-client-cert-identity",
        description="Header the proxy sets to the verified client identity (e.g. a SPIFFE URI).",
    )
    trusted_proxies: tuple[str, ...] = Field(
        default=(), description="CIDRs of the proxies allowed to set the header."
    )
    identities: dict[str, ServicePrincipalSettings] = Field(
        default_factory=dict,
        description="Exact identity value -> principal. Unlisted identities are rejected.",
    )

    @model_validator(mode="after")
    def validate_proxy(self) -> TrustedProxyIdentitySettings:
        if not _HEADER_NAME.match(self.header):
            raise ValueError("trusted_proxy_identity.header is not a valid header name")
        if self.header.lower() in _RESERVED_IDENTITY_HEADERS:
            raise ValueError(f"trusted_proxy_identity.header may not be {self.header!r}")
        for cidr in self.trusted_proxies:
            try:
                network = ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise ValueError(f"trusted proxy {cidr!r} is not an IP network") from exc
            if network.prefixlen == 0:
                raise ValueError("trusted proxies may not include every address")
        if self.enabled and not self.trusted_proxies:
            raise ValueError("trusted_proxy_identity requires trusted_proxies")
        if self.enabled and not self.identities:
            raise ValueError("trusted_proxy_identity requires identities")
        return self


class AuthenticationAuditSettings(_AuthSettings):
    """Audit records for authentication outcomes (never the token)."""

    record_failures: bool = True
    record_success: bool = Field(
        default=False, description="Also record each successful authentication (high volume)."
    )
    max_failure_records_per_minute: int = Field(
        default=600,
        ge=1,
        description="Cap on failure records per process per minute; the excess is counted in "
        "one auth.audit.suppressed record so a credential flood cannot flood the audit store.",
    )
