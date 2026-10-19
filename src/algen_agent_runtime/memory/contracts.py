from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Literal, Protocol

from pydantic import Field, model_validator

from algen_agent_runtime.types.contracts import StrictModel

MemoryKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]


class MemoryScope(StrictModel):
    tenant_id: str
    user_id: str
    agent_id: str


class MemoryWrite(StrictModel):
    key: MemoryKey
    expected_revision: int = Field(ge=0, strict=True)
    kind: Literal["fact", "preference", "procedure"]
    content: str = Field(min_length=1, max_length=2048)
    evidence: str = Field(min_length=1, max_length=1024)
    retention_seconds: int = Field(default=86400, ge=60, le=31536000, strict=True)


class MemoryDelete(StrictModel):
    key: MemoryKey
    expected_revision: int = Field(ge=1, strict=True)


class MemoryRecall(StrictModel):
    key: MemoryKey | None = None
    query: str = Field(default="", max_length=256)
    limit: int = Field(default=5, ge=1, le=20, strict=True)

    @model_validator(mode="after")
    def one_lookup(self) -> MemoryRecall:
        if self.key is not None and self.query:
            raise ValueError("use either key or query")
        return self


class LearnedMemory(StrictModel):
    key: MemoryKey
    revision: int = Field(ge=1)
    kind: Literal["fact", "preference", "procedure"] | None
    content: str | None = Field(max_length=2048)
    evidence: str | None = Field(max_length=1024)
    deleted: bool = False
    source_run_id: str
    source_step_id: str
    source_agent_version: str
    updated_at: datetime
    expires_at: datetime | None


class MemoryReceipt(StrictModel):
    key: MemoryKey
    revision: int = Field(ge=1)
    deleted: bool


class MemoryMutation(StrictModel):
    scope: MemoryScope
    operation_id: str
    expected_revision: int = Field(ge=0)
    record: LearnedMemory

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    @model_validator(mode="after")
    def valid_mutation(self) -> MemoryMutation:
        record = self.record
        if record.revision != self.expected_revision + 1:
            raise ValueError("memory revision must advance by one")
        if record.deleted:
            if (
                self.expected_revision < 1
                or record.content is not None
                or record.evidence is not None
                or record.expires_at is not None
            ):
                raise ValueError(
                    "delete requires an existing revision and clears content and expiry"
                )
        elif (
            record.kind is None
            or not record.content
            or len(record.content) > 2048
            or not record.evidence
            or len(record.evidence) > 1024
            or record.expires_at is None
        ):
            raise ValueError("active memory requires bounded content, evidence, and expiry")
        return self


class LearnedMemoryStore(Protocol):
    async def get(self, scope: MemoryScope, key: str) -> LearnedMemory | None: ...
    async def search(
        self, scope: MemoryScope, query: str, limit: int
    ) -> tuple[LearnedMemory, ...]: ...
    async def apply(self, mutation: MemoryMutation) -> MemoryReceipt: ...
    async def purge_expired(self, limit: int = 1000) -> int: ...
