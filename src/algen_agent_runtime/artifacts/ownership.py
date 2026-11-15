"""Who may use an artifact: its owner, the user of its run, or an artifacts administrator.

An artifact belongs to a user of its tenant (``Artifact.user_id``): the uploader, or the user of
the run that produced it. The API routes apply :func:`artifact_owned_by` and, for listing,
:func:`list_user_artifacts`; a host that serves artifacts itself applies them the same way.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from algen_agent_runtime.types.contracts import Artifact, ArtifactDescriptor, ArtifactStatus

ARTIFACTS_ADMIN_SCOPE = "artifacts:admin"
"""The scope that lets a principal list, read and delete any artifact of its tenant."""


def artifact_owned_by(
    artifact: Artifact | ArtifactDescriptor, user_id: str, *, run_user_id: str | None = None
) -> bool:
    """Whether ``user_id`` may use ``artifact`` as its user.

    True when the artifact names ``user_id`` as its owner, or when ``run_user_id`` (the user of
    the artifact's run, which the caller looks up: the conversation's owner for a conversation
    turn, else the run's ``user_id``) is ``user_id``. An artifact stored before owners were
    recorded (``user_id`` is ``None``) and with no run belongs to nobody: only a principal with
    ``artifacts:admin`` may use it.
    """
    if not user_id:
        return False
    if artifact.user_id is not None and artifact.user_id == user_id:
        return True
    return artifact.run_id is not None and run_user_id is not None and run_user_id == user_id


async def list_user_artifacts(
    store: Any,
    tenant_id: str,
    user_id: str,
    *,
    run_id: str | None = None,
    status: ArtifactStatus | None = None,
    limit: int = 100,
) -> Sequence[ArtifactDescriptor]:
    """The artifacts ``user_id`` owns in the tenant, newest first.

    Uses the store's ``list_for_user`` (the memory, PostgreSQL and S3 stores have it). A store
    without it is listed for the tenant and filtered by owner, which may return fewer than
    ``limit`` and finds nothing in a store that does not keep ``user_id``.
    """
    list_for_user = getattr(store, "list_for_user", None)
    if list_for_user is not None:
        return tuple(
            await list_for_user(tenant_id, user_id, run_id=run_id, status=status, limit=limit)
        )
    listed = await store.list(tenant_id, run_id=run_id, status=status, limit=limit)
    return tuple(item for item in listed if item.user_id == user_id)
