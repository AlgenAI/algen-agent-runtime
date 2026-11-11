"""A webhook toolkit with one approval-gated write. It never returns the secret it is given."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel

from algen_agent_runtime.tools.authoring import Toolkit, write

webhooks = Toolkit("webhooks")


class Registration(BaseModel):
    url: str
    secret_fingerprint: str


@webhooks.tool(write(approval=True))
async def register(url: str, secret: str) -> Registration:
    """Register an outbound webhook signed with ``secret``. An approver agrees to every call."""
    # Never return the secret itself: results reach the model and the run's events.
    fingerprint = hashlib.sha256(secret.encode()).hexdigest()[:12]
    return Registration(url=url, secret_fingerprint=fingerprint)
