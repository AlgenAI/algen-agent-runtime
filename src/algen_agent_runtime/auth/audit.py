"""Content-minimal authentication audit records, logs and metrics.

A record carries at most the method, the failure reason, the issuer, the key id and the
subject; never the token, a header value other than a mapped service identity, or claims.
Failures without a verified tenant are recorded under the ``_authentication`` tenant, so an
unverified tenant claim cannot write into a real tenant's audit trail.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from algen_agent_runtime.auth.contracts import AuthenticatedIdentity, AuthenticationFailure
from algen_agent_runtime.config.authentication import AuthenticationAuditSettings
from algen_agent_runtime.events.contracts import AuditEvent
from algen_agent_runtime.types.interfaces import AuditLog

logger = logging.getLogger(__name__)

#: Audit tenant for authentication failures that have no verified tenant.
AUTHENTICATION_AUDIT_TENANT = "_authentication"
AUTH_AUDIT_ACTION = "auth.authenticate"
AUTH_AUDIT_SUPPRESSED_ACTION = "auth.audit.suppressed"
AUTH_METRIC_NAME = "algen_agent_runtime.auth.attempts"


def _counter() -> Any:
    try:
        from opentelemetry import metrics

        return metrics.get_meter("algen_agent_runtime.auth").create_counter(
            AUTH_METRIC_NAME,
            unit="1",
            description="API authentication attempts by method, outcome and failure reason",
        )
    except Exception:
        return None


class AuthenticationAuditor:
    def __init__(
        self,
        audits: AuditLog | None,
        settings: AuthenticationAuditSettings | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._audits = audits
        self._settings = settings or AuthenticationAuditSettings()
        self._clock = clock
        self._window_start = clock()
        self._window_count = 0
        self._suppressed = 0
        self._counter = _counter()

    def _count(self, method: str, outcome: str, reason: str | None) -> None:
        if self._counter is None:
            return
        attributes = {"method": method, "outcome": outcome}
        if reason:
            attributes["reason"] = reason
        try:
            self._counter.add(1, attributes)
        except Exception:
            pass

    async def _append(self, event: AuditEvent) -> None:
        if self._audits is None:
            return
        try:
            await self._audits.append(event)
        except Exception:
            logger.exception("failed to append authentication audit record")

    async def success(self, identity: AuthenticatedIdentity) -> None:
        self._count(identity.method, "success", None)
        if not self._settings.record_success:
            return
        metadata = {
            "method": identity.method,
            "issuer": identity.issuer,
            "kid": identity.key_id,
            "subject": identity.user_id,
        }
        await self._append(
            AuditEvent(
                action=AUTH_AUDIT_ACTION,
                outcome="success",
                tenant_id=identity.tenant_id,
                actor_id=identity.user_id,
                resource_id=identity.method,
                metadata={key: value for key, value in metadata.items() if value},
            )
        )

    async def failure(self, failure: AuthenticationFailure) -> None:
        self._count(failure.method, "failure", failure.reason)
        logger.warning(
            "authentication failed",
            extra={
                "auth_method": failure.method,
                "auth_reason": failure.reason,
                "auth_issuer": failure.issuer,
                "auth_kid": failure.key_id,
            },
        )
        if not self._settings.record_failures:
            return
        now = self._clock()
        if now - self._window_start >= 60:
            suppressed = self._suppressed
            self._window_start, self._window_count, self._suppressed = now, 0, 0
            if suppressed:
                await self._append(
                    AuditEvent(
                        action=AUTH_AUDIT_SUPPRESSED_ACTION,
                        outcome="suppressed",
                        tenant_id=AUTHENTICATION_AUDIT_TENANT,
                        actor_id="",
                        resource_id="api",
                        metadata={"count": suppressed},
                    )
                )
        if self._window_count >= self._settings.max_failure_records_per_minute:
            self._suppressed += 1
            return
        self._window_count += 1
        metadata = {
            "method": failure.method,
            "reason": failure.reason,
            "issuer": failure.issuer,
            "kid": failure.key_id,
            "subject": failure.subject,
        }
        await self._append(
            AuditEvent(
                action=AUTH_AUDIT_ACTION,
                outcome="failure",
                tenant_id=failure.tenant_id or AUTHENTICATION_AUDIT_TENANT,
                actor_id=failure.subject or "",
                resource_id=failure.method,
                metadata={key: value for key, value in metadata.items() if value},
            )
        )
