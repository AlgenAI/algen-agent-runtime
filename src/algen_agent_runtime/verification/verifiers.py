from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import jsonschema
from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.types.interfaces import Verifier


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    passed: bool
    confidence: float = Field(ge=0, le=1)
    reason_code: str
    message: str
    repair_hint: str | None = None


class NonEmptyVerifier:
    name = "non_empty"

    async def verify(self, value: Any, context: Mapping[str, Any]) -> VerificationResult:
        passed = bool(value and str(value).strip())
        return VerificationResult(
            passed=passed,
            confidence=1.0 if passed else 0.0,
            reason_code="output.non_empty" if passed else "output.empty",
            message="Output is non-empty." if passed else "Output is empty.",
            repair_hint=None if passed else "Produce a concrete response.",
        )


class JsonSchemaVerifier:
    name = "json_schema"

    async def verify(self, value: Any, context: Mapping[str, Any]) -> VerificationResult:
        schema = context.get("schema")
        if not schema:
            return VerificationResult(
                passed=True,
                confidence=1,
                reason_code="schema.not_requested",
                message="No schema requested.",
            )
        try:
            candidate = json.loads(value) if isinstance(value, str) else value
            jsonschema.validate(candidate, schema)
            return VerificationResult(
                passed=True,
                confidence=1,
                reason_code="schema.valid",
                message="Output matches schema.",
            )
        except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
            return VerificationResult(
                passed=False,
                confidence=0,
                reason_code="schema.invalid",
                message=str(exc),
                repair_hint="Return only JSON matching the supplied schema.",
            )


class CitationVerifier:
    name = "citations"
    _marker = re.compile(r"\[(S\d+)\]")

    async def verify(self, value: Any, context: Mapping[str, Any]) -> VerificationResult:
        state = context.get("state")
        citations = tuple(getattr(state, "citations", ()))
        if not citations:
            return VerificationResult(
                passed=True,
                confidence=1,
                reason_code="citations.no_sources",
                message="No retrieved sources require citation.",
            )
        known = {citation.id for citation in citations}
        referenced = set(self._marker.findall(str(value or "")))
        unknown = referenced - known
        if unknown:
            return VerificationResult(
                passed=False,
                confidence=0,
                reason_code="citations.unknown",
                message=f"Unknown citation identifiers: {', '.join(sorted(unknown))}.",
                repair_hint="Use only source identifiers supplied in retrieved context.",
            )
        used = referenced & known
        if not used:
            return VerificationResult(
                passed=False,
                confidence=0,
                reason_code="citations.missing",
                message="Retrieved evidence was available but no source was cited.",
                repair_hint="Cite grounded claims with source identifiers such as [S1].",
            )
        return VerificationResult(
            passed=True,
            confidence=len(used) / len(known),
            reason_code="citations.valid",
            message="All citation identifiers refer to retrieved evidence.",
        )


class VerificationService:
    def __init__(self, verifiers: Sequence[Verifier] | None = None) -> None:
        selected = verifiers or (
            NonEmptyVerifier(),
            JsonSchemaVerifier(),
            CitationVerifier(),
        )
        self._items: dict[str, Verifier] = {verifier.name: verifier for verifier in selected}

    def register(self, verifier: Verifier) -> None:
        self._items[verifier.name] = verifier

    async def verify(
        self, names: Sequence[str], value: Any, context: Mapping[str, Any]
    ) -> tuple[VerificationResult, ...]:
        results = []
        for name in names:
            if name not in self._items:
                raise KeyError(f"verifier {name!r} is not registered")
            results.append(await self._items[name].verify(value, context))
        return tuple(results)
