"""Service identity forwarded by a trusted TLS-terminating proxy (mTLS at the edge).

The proxy verifies the client certificate and sets one header to the client's identity (for
example a SPIFFE URI SAN). The Runtime honours that header only when the request's direct TCP
peer is inside a configured proxy CIDR, maps the exact identity to a configured principal, and
rejects (and audits) the header from any other peer. The proxy must overwrite the header on
every request so a client cannot supply its own value.

The peer address is the ASGI ``client``. A server that rewrites it from ``X-Forwarded-For``
(uvicorn ``proxy_headers``) would let a spoofed forwarding header pick the peer, so
``algen-agent-runtime serve`` disables proxy-header rewriting when this hook is enabled; other
servers must be configured the same way.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence

from algen_agent_runtime.auth.contracts import AuthenticatedIdentity, AuthenticationFailure
from algen_agent_runtime.config.authentication import TrustedProxyIdentitySettings

_MAX_IDENTITY_LENGTH = 1024


class TrustedProxyIdentity:
    def __init__(self, settings: TrustedProxyIdentitySettings) -> None:
        self.header = settings.header.lower()
        self._networks = tuple(
            ipaddress.ip_network(cidr, strict=False) for cidr in settings.trusted_proxies
        )
        self._identities = dict(settings.identities)

    def is_trusted_peer(self, peer: str | None) -> bool:
        if not peer:
            return False
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return False
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return any(address in network for network in self._networks)

    def resolve(self, values: Sequence[str], peer: str | None) -> AuthenticatedIdentity | None:
        """The service principal, ``None`` when the header is absent, or a failure."""
        if not values:
            return None
        if not self.is_trusted_peer(peer):
            raise AuthenticationFailure(
                "untrusted_proxy", method="trusted_proxy", detail="service identity not accepted"
            )
        if len(values) != 1:
            raise AuthenticationFailure(
                "invalid_service_identity",
                method="trusted_proxy",
                detail="service identity not accepted",
            )
        identity = values[0].strip()
        if not identity or len(identity) > _MAX_IDENTITY_LENGTH or not identity.isprintable():
            raise AuthenticationFailure(
                "invalid_service_identity",
                method="trusted_proxy",
                detail="service identity not accepted",
            )
        principal = self._identities.get(identity)
        if principal is None:
            raise AuthenticationFailure(
                "unknown_service_identity",
                method="trusted_proxy",
                subject=identity,
                detail="service identity not accepted",
            )
        return AuthenticatedIdentity(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id or identity,
            scopes=frozenset(principal.scopes),
            method="trusted_proxy",
        )
