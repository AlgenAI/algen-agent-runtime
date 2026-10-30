"""In-process stand-ins for the identity provider and an upstream API. Nothing touches the network.

Needs the ``auth`` extra (PyJWT with ``cryptography``); ``app.py`` imports this module only after
checking that both are installed. The signing key is generated when the example starts and never
leaves the process.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec

ISSUER = "https://idp.example.com/realms/demo"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"
RUNTIME_AUDIENCE = "algen-agent-runtime"
LMS_AUDIENCE = "lms-api"
KEY_ID = "demo-signing-key"


@dataclass(frozen=True)
class IssuedToken:
    """A signed access token and the claims in it (``jti`` and ``exp`` drive revocation)."""

    token: str
    claims: dict[str, Any]

    @property
    def header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


class LocalIdentityProvider:
    """An OIDC issuer: a discovery document, a JWKS with one EC P-256 key, and a token mint."""

    def __init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self._key.public_key()))
        self.jwks = {"keys": [{**jwk, "kid": KEY_ID, "use": "sig", "alg": "ES256"}]}
        self.fetches: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.fetches.append(url)
        if url == DISCOVERY_URL:
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URL})
        if url == JWKS_URL:
            return httpx.Response(200, json=self.jwks)
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def public_key(self) -> ec.EllipticCurvePublicKey:
        return self._key.public_key()

    def forge(self, subject: str, **claims: Any) -> IssuedToken:
        """A token signed by some other key under this provider's key id: a forgery."""
        return self.issue(subject, signing_key=ec.generate_private_key(ec.SECP256R1()), **claims)

    def issue(self, subject: str, *, signing_key: Any = None, **claims: Any) -> IssuedToken:
        """A 10-minute token for ``subject`` in tenant ``acme``, valid for the Runtime and the LMS."""
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": ISSUER,
            "aud": [RUNTIME_AUDIENCE, LMS_AUDIENCE],
            "sub": subject,
            "tenant_id": "acme",
            "iat": now,
            "exp": now + 600,
            "jti": str(uuid.uuid4()),
            **claims,
        }
        token = jwt.encode(
            payload, signing_key or self._key, algorithm="ES256", headers={"kid": KEY_ID}
        )
        return IssuedToken(token=token, claims=payload)


@dataclass
class FakeLms:
    """An upstream API that authenticates the user itself: it accepts only tokens issued for it."""

    identity_provider: LocalIdentityProvider
    enrolments: dict[str, list[str]] = field(default_factory=dict)
    seen: list[str] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme != "Bearer" or not token:
            self.seen.append("<no credential>")
            return httpx.Response(401, json={"error": "sign in"})
        try:
            claims = jwt.decode(
                token,
                self.identity_provider.public_key(),
                algorithms=["ES256"],
                audience=LMS_AUDIENCE,
                issuer=ISSUER,
            )
        except jwt.PyJWTError:
            self.seen.append("<invalid credential>")
            return httpx.Response(401, json={"error": "invalid token"})
        user = str(claims["sub"])
        self.seen.append(user)
        return httpx.Response(200, json={"user": user, "courses": self.enrolments.get(user, [])})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
