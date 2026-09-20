from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RetrievalMode(StrEnum):
    VECTOR = "vector"
    KEYWORD = "keyword"
    HYBRID = "hybrid"


class RetrievalQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str
    tenant_id: str
    mode: RetrievalMode = RetrievalMode.HYBRID
    limit: int = Field(default=10, ge=1, le=100)
    filters: dict[str, Any] = Field(default_factory=dict)
    keyword_weight: float = Field(default=0.4, ge=0, le=1)
    vector_weight: float = Field(default=0.6, ge=0, le=1)
    minimum_score: float = Field(default=0.0, ge=0, le=1)

    @property
    def normalized_weights(self) -> tuple[float, float]:
        if self.mode == RetrievalMode.KEYWORD:
            return 1.0, 0.0
        if self.mode == RetrievalMode.VECTOR:
            return 0.0, 1.0
        total = self.keyword_weight + self.vector_weight
        if total == 0:
            return 0.5, 0.5
        return self.keyword_weight / total, self.vector_weight / total


class SourceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    text: str = Field(min_length=1)
    source: str
    title: str | None = None
    uri: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    text: str
    source: str
    score: float = Field(ge=0, le=1)
    document_id: str | None = None
    title: str | None = None
    uri: str | None = None
    chunk_index: int = Field(default=0, ge=0)
    keyword_score: float = Field(default=0, ge=0, le=1)
    vector_score: float = Field(default=0, ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
