"""Admin routes for bearer token revocation (``api.token_revocation``).

``POST /v1/auth/revocations/tokens`` denies one token by its ``jti`` until its expiry, and
``POST /v1/auth/revocations/subjects`` refuses every token for a subject issued at or before a
cutoff (default: now). Both need the ``auth:revoke`` scope and act only within the caller's
tenant. They return 404 when revocation is not enabled. Each revocation is audited; the token
itself is never sent or stored.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from algen_agent_runtime.api.dependencies import Principal, require_scope
from algen_agent_runtime.auth.revocation import TokenRevocationStore
from algen_agent_runtime.events.contracts import AuditEvent
from algen_agent_runtime.types.contracts import utc_now

if TYPE_CHECKING:
    from algen_agent_runtime.orchestration.container import Container

# A revocation cutoff may sit slightly ahead of this node's clock, never far in the future
# (that would turn a revocation into an open-ended ban that later logins cannot clear).
_MAX_CUTOFF_SKEW = timedelta(minutes=5)
# jti entries live until the token expires; nothing legitimate needs to be kept for longer.
_MAX_TOKEN_EXPIRY = timedelta(days=366)


class RevokeTokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jti: str = Field(min_length=1, max_length=512, description="The token's jti claim.")
    expires_at: AwareDatetime = Field(
        description="The token's exp; the denylist entry is purged after it."
    )
    reason: str | None = Field(default=None, max_length=200)


class RevokeSubjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=512, description="The principal's user id.")
    revoked_before: AwareDatetime | None = Field(
        default=None, description="Tokens issued at or before this are refused; default now."
    )
    reason: str | None = Field(default=None, max_length=200)


def add_auth_routes(app: FastAPI, container: Container, identity: Any) -> None:
    def store() -> TokenRevocationStore:
        if container.token_revocations is None:
            raise HTTPException(status_code=404, detail="token revocation is not enabled")
        return container.token_revocations

    async def audit(principal: Principal, action: str, resource: str, **metadata: Any) -> None:
        await container.runtime.audits.append(
            AuditEvent(
                action=action,
                outcome="completed",
                tenant_id=principal.tenant_id,
                actor_id=principal.user_id,
                resource_id=resource,
                metadata={key: value for key, value in metadata.items() if value is not None},
            )
        )

    @app.post("/v1/auth/revocations/tokens")
    async def revoke_token(
        body: RevokeTokenBody, principal: Principal = identity
    ) -> dict[str, Any]:
        require_scope(principal, "auth:revoke")
        revocations = store()
        if body.expires_at > utc_now() + _MAX_TOKEN_EXPIRY:
            raise HTTPException(status_code=422, detail="expires_at is too far in the future")
        await revocations.revoke_token(
            principal.tenant_id, body.jti, body.expires_at, reason=body.reason
        )
        await audit(
            principal,
            "auth.token.revoked",
            body.jti,
            expires_at=body.expires_at.isoformat(),
            reason=body.reason,
        )
        return {"revoked": True, "jti": body.jti, "expires_at": body.expires_at.isoformat()}

    @app.post("/v1/auth/revocations/subjects")
    async def revoke_subject(
        body: RevokeSubjectBody, principal: Principal = identity
    ) -> dict[str, Any]:
        require_scope(principal, "auth:revoke")
        revocations = store()
        now = utc_now()
        cutoff: datetime = body.revoked_before or now
        if cutoff > now + _MAX_CUTOFF_SKEW:
            raise HTTPException(status_code=422, detail="revoked_before may not be in the future")
        await revocations.revoke_subject(
            principal.tenant_id, body.subject, cutoff, reason=body.reason
        )
        await audit(
            principal,
            "auth.subject.revoked",
            body.subject,
            revoked_before=cutoff.isoformat(),
            reason=body.reason,
        )
        return {"revoked": True, "subject": body.subject, "revoked_before": cutoff.isoformat()}
