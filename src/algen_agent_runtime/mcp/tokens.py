"""Where the Runtime keeps MCP OAuth tokens and in-flight authorizations.

Tokens are keyed by (tenant, user or None, server): a lookup always names its tenant, so one
tenant can never read another's tokens. ``PostgresMCPTokenStore`` encrypts every token set and
every pending authorization with a ``SecretCipher`` whose associated data is the row's key, so a
ciphertext moved to another row (or tenant) fails to decrypt. ``InMemoryMCPTokenStore`` keeps
them in process memory only, for development and single-process deployments.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pydantic import SecretStr

from algen_agent_runtime.security.encryption import SecretCipher
from algen_agent_runtime.types.contracts import StrictModel, utc_now


@dataclass(frozen=True)
class MCPTokenKey:
    """Whose token: a tenant's (``user_id`` None) or one user's in a tenant."""

    tenant_id: str
    user_id: str | None
    server: str

    def associated_data(self) -> bytes:
        return "\x1f".join(
            ("mcp-oauth-token", self.tenant_id, self.user_id or "", self.server)
        ).encode()


class MCPOAuthTokenSet(StrictModel):
    """A stored token set. The secrets are ``SecretStr`` so they never print."""

    access_token: SecretStr
    refresh_token: SecretStr | None = None
    token_type: str = "Bearer"
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = ()
    connected_at: datetime
    connected_by: str

    def to_plain_json(self) -> str:
        """The value the store encrypts; only ever handed to a cipher."""
        return json.dumps(
            {
                "access_token": self.access_token.get_secret_value(),
                "refresh_token": self.refresh_token.get_secret_value()
                if self.refresh_token
                else None,
                "token_type": self.token_type,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
                "scopes": list(self.scopes),
                "connected_at": self.connected_at.isoformat(),
                "connected_by": self.connected_by,
            }
        )


class MCPOAuthPendingAuthorization(StrictModel):
    """An authorization begun by the start route and not yet finished by the callback."""

    state_hash: str
    server: str
    tenant_id: str
    initiated_by: str
    token_user_id: str | None
    code_verifier: SecretStr
    redirect_uri: str
    expires_at: datetime

    def to_plain_json(self) -> str:
        return json.dumps(
            {
                **self.model_dump(mode="json", exclude={"code_verifier"}),
                "code_verifier": self.code_verifier.get_secret_value(),
            }
        )


class MCPTokenStore(Protocol):
    """Persistence for MCP OAuth tokens and single-use authorization states."""

    async def get(self, key: MCPTokenKey) -> MCPOAuthTokenSet | None: ...

    async def put(self, key: MCPTokenKey, tokens: MCPOAuthTokenSet) -> None: ...

    async def delete(self, key: MCPTokenKey) -> bool: ...

    async def put_pending(self, pending: MCPOAuthPendingAuthorization) -> None: ...

    async def take_pending(self, state_hash: str) -> MCPOAuthPendingAuthorization | None:
        """Remove and return the pending authorization: a state works once."""
        ...


class InMemoryMCPTokenStore:
    """Process-local token store. A restart forgets every connection."""

    def __init__(self) -> None:
        self._tokens: dict[MCPTokenKey, MCPOAuthTokenSet] = {}
        self._pending: dict[str, MCPOAuthPendingAuthorization] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: MCPTokenKey) -> MCPOAuthTokenSet | None:
        return self._tokens.get(key)

    async def put(self, key: MCPTokenKey, tokens: MCPOAuthTokenSet) -> None:
        self._tokens[key] = tokens

    async def delete(self, key: MCPTokenKey) -> bool:
        return self._tokens.pop(key, None) is not None

    async def put_pending(self, pending: MCPOAuthPendingAuthorization) -> None:
        async with self._lock:
            now = utc_now()
            for state_hash in [k for k, v in self._pending.items() if v.expires_at <= now]:
                del self._pending[state_hash]
            self._pending[pending.state_hash] = pending

    async def take_pending(self, state_hash: str) -> MCPOAuthPendingAuthorization | None:
        async with self._lock:
            return self._pending.pop(state_hash, None)


class PostgresMCPTokenStore:
    """Encrypted token store in PostgreSQL (migration ``012_mcp_oauth.sql``)."""

    def __init__(self, database: Any, cipher: SecretCipher) -> None:
        self._database = database
        self._cipher = cipher

    async def get(self, key: MCPTokenKey) -> MCPOAuthTokenSet | None:
        row = await self._database.fetchrow(
            "SELECT ciphertext FROM algen_agent_runtime_mcp_oauth_tokens "
            "WHERE tenant_id=$1 AND user_id=$2 AND server=$3",
            key.tenant_id,
            key.user_id or "",
            key.server,
        )
        if row is None:
            return None
        plain = self._cipher.decrypt(row["ciphertext"], key.associated_data())
        return MCPOAuthTokenSet.model_validate_json(plain)

    async def put(self, key: MCPTokenKey, tokens: MCPOAuthTokenSet) -> None:
        ciphertext = self._cipher.encrypt(tokens.to_plain_json().encode(), key.associated_data())
        await self._database.execute(
            "INSERT INTO algen_agent_runtime_mcp_oauth_tokens "
            "(tenant_id,user_id,server,ciphertext,updated_at) VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT (tenant_id,user_id,server) DO UPDATE "
            "SET ciphertext=EXCLUDED.ciphertext, updated_at=EXCLUDED.updated_at",
            key.tenant_id,
            key.user_id or "",
            key.server,
            ciphertext,
            utc_now(),
        )

    async def delete(self, key: MCPTokenKey) -> bool:
        row = await self._database.fetchrow(
            "DELETE FROM algen_agent_runtime_mcp_oauth_tokens "
            "WHERE tenant_id=$1 AND user_id=$2 AND server=$3 RETURNING server",
            key.tenant_id,
            key.user_id or "",
            key.server,
        )
        return row is not None

    async def put_pending(self, pending: MCPOAuthPendingAuthorization) -> None:
        await self._database.execute(
            "DELETE FROM algen_agent_runtime_mcp_oauth_states WHERE expires_at <= $1", utc_now()
        )
        ciphertext = self._cipher.encrypt(
            pending.to_plain_json().encode(), _pending_associated_data(pending.state_hash)
        )
        await self._database.execute(
            "INSERT INTO algen_agent_runtime_mcp_oauth_states "
            "(state_hash,tenant_id,server,ciphertext,expires_at) VALUES ($1,$2,$3,$4,$5)",
            pending.state_hash,
            pending.tenant_id,
            pending.server,
            ciphertext,
            pending.expires_at,
        )

    async def take_pending(self, state_hash: str) -> MCPOAuthPendingAuthorization | None:
        # DELETE ... RETURNING is atomic: two callbacks racing on one state get it once.
        row = await self._database.fetchrow(
            "DELETE FROM algen_agent_runtime_mcp_oauth_states WHERE state_hash=$1 "
            "RETURNING ciphertext",
            state_hash,
        )
        if row is None:
            return None
        plain = self._cipher.decrypt(row["ciphertext"], _pending_associated_data(state_hash))
        return MCPOAuthPendingAuthorization.model_validate_json(plain)


def _pending_associated_data(state_hash: str) -> bytes:
    return f"mcp-oauth-state\x1f{state_hash}".encode()
