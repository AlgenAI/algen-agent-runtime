from __future__ import annotations

from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class JevQuestion(BaseModel):
    """A bounded TypeSafe System One question."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["choice", "score", "noul"]
    instructions: str = Field(min_length=1, max_length=4_000)
    criteria: dict[str, str] | tuple[str, ...] | None = None

    @model_validator(mode="after")
    def validate_criteria(self) -> JevQuestion:
        if self.type == "choice":
            if not isinstance(self.criteria, dict) or len(self.criteria) < 2:
                raise ValueError("choice questions require at least two named criteria")
        elif self.type == "score":
            if not isinstance(self.criteria, tuple) or len(self.criteria) < 2:
                raise ValueError("score questions require at least two ordered criteria")
        elif self.criteria is not None:
            raise ValueError("noul questions do not accept criteria")
        return self


class JevRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    state: str | dict[str, Any] | list[Any]
    questions: dict[str, JevQuestion] = Field(min_length=1, max_length=64)
    model: str = Field(default="jev-latest", min_length=1, max_length=128)


class JevAnswer(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    type: Literal["choice", "score", "noul"]
    choice: str | None = None
    score: float | None = None
    noul: float | None = Field(default=None, ge=0, le=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    probabilities: dict[str, float] = Field(default_factory=dict)

    @property
    def decision_confidence(self) -> float:
        if self.confidence is not None:
            return self.confidence
        if self.type == "noul" and self.noul is not None:
            return max(self.noul, 1 - self.noul)
        return max(self.probabilities.values(), default=0.0)


class JevUsage(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class JevResponse(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    model: str
    answers: dict[str, JevAnswer]
    usage: JevUsage = Field(default_factory=JevUsage)


class JevSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    api_key: SecretStr
    base_url: str = "https://api.typesafe.ai"
    timeout_seconds: float = Field(default=15, gt=0, le=120)


class JevDecisionError(RuntimeError):
    """Content-minimal TypeSafe transport error safe to expose in logs."""


class JevDecisionClient:
    """Typed client for TypeSafe's Jev System One decision endpoint."""

    def __init__(
        self, settings: JevSettings, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=settings.timeout_seconds)
        self._owns_client = client is None

    async def decide(self, request: JevRequest) -> JevResponse:
        try:
            response = await self._client.post(
                f"{self._settings.base_url.rstrip('/')}/v1/systemone",
                headers={
                    "Authorization": f"Bearer {self._settings.api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                json=request.model_dump(mode="json", exclude_none=True),
            )
            response.raise_for_status()
            return JevResponse.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise JevDecisionError(
                f"Jev decision request failed ({type(exc).__name__})"
            ) from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
