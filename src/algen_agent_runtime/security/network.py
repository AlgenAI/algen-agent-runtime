from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from algen_agent_runtime.exceptions.errors import PolicyDeniedError


async def validate_outbound_url(
    url: str,
    allowed_hosts: tuple[str, ...],
    allow_private_networks: bool = False,
) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PolicyDeniedError("outbound URL must use http or https and include a host")
    hostname = parsed.hostname.lower().rstrip(".")
    if allowed_hosts and not any(
        hostname == host or hostname.endswith("." + host) for host in allowed_hosts
    ):
        raise PolicyDeniedError(f"outbound host {hostname!r} is not allowlisted")
    try:
        addresses = (
            await __import__("asyncio")
            .get_running_loop()
            .run_in_executor(
                None,
                lambda: socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM),
            )
        )
    except socket.gaierror as exc:
        raise PolicyDeniedError("outbound host could not be resolved") from exc
    if not allow_private_networks:
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
            ):
                raise PolicyDeniedError("outbound URL resolves to a non-public address")
