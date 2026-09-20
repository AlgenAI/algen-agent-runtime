from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class CacheScope(StrEnum):
    GLOBAL = "global"
    TENANT = "tenant"
    USER = "user"
    SESSION = "session"
    RUN = "run"


class CacheContext(BaseModel):
    """Identity boundary used to prevent cache data crossing authorization scopes."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    authorization_fingerprint: str | None = None


class CachePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    scope: CacheScope = CacheScope.TENANT
    ttl_seconds: int = Field(default=300, ge=1, le=2_592_000)
    maximum_value_bytes: int = Field(default=1_048_576, ge=1, le=100_000_000)


class CacheStore(Protocol):
    backend_name: str

    async def get(self, key: str) -> bytes | None: ...

    async def set(
        self, key: str, value: bytes, ttl_seconds: int, tags: tuple[str, ...] = ()
    ) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def invalidate_tags(self, tags: tuple[str, ...]) -> int: ...

    async def health(self) -> bool: ...
