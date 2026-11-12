"""Admin routes that export or erase one user's data (``persistence/user_data.py``).

``GET /v1/admin/users/{user_id}/data`` returns everything the Runtime stores about the user in
the caller's tenant (``include_artifact_data=true`` adds each file's bytes, base64). ``DELETE``
on the same path erases it and returns the counts per kind; ``dry_run=true`` only counts and
lists what blocks the erasure. A user with work in progress gets 409 and nothing is deleted.
Both need the ``users:admin`` scope, act only within the caller's tenant and are audited with
the caller as the actor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException

from algen_agent_runtime.api.dependencies import Principal, require_scope
from algen_agent_runtime.persistence.user_data import UserDataService

if TYPE_CHECKING:
    from algen_agent_runtime.orchestration.container import Container

USER_ADMIN_SCOPE = "users:admin"


def add_user_data_routes(app: FastAPI, container: Container, identity: Any) -> None:
    def service() -> UserDataService:
        if container.user_data is None:
            raise HTTPException(status_code=404, detail="user data export is not available")
        return container.user_data

    def check_user(user_id: str) -> str:
        if not user_id or len(user_id) > 512:
            raise HTTPException(status_code=422, detail="invalid user id")
        return user_id

    @app.get("/v1/admin/users/{user_id}/data")
    async def export_user_data(
        user_id: str, include_artifact_data: bool = False, principal: Principal = identity
    ) -> dict[str, Any]:
        require_scope(principal, USER_ADMIN_SCOPE)
        return await service().export_user(
            principal.tenant_id,
            check_user(user_id),
            include_artifact_data=include_artifact_data,
            actor_id=principal.user_id,
        )

    @app.delete("/v1/admin/users/{user_id}/data")
    async def erase_user_data(
        user_id: str, dry_run: bool = False, principal: Principal = identity
    ) -> dict[str, Any]:
        require_scope(principal, USER_ADMIN_SCOPE)
        report = await service().erase_user(
            principal.tenant_id, check_user(user_id), dry_run=dry_run, actor_id=principal.user_id
        )
        return report.model_dump(mode="json")
