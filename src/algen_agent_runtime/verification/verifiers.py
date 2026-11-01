from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
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
    # What failed, as paths and constraints only, never the output's values: safe to show the
    # model in a repair request and to record.
    findings: tuple[str, ...] = ()


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
        except json.JSONDecodeError as exc:
            findings: tuple[str, ...] = (
                f"not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}",
            )
        except jsonschema.ValidationError:
            findings = schema_findings(candidate, schema)
        return VerificationResult(
            passed=False,
            confidence=0,
            reason_code="schema.invalid",
            message="Output does not match the schema: " + "; ".join(findings),
            repair_hint="Return only JSON matching the supplied schema.",
            findings=findings,
        )


def schema_findings(value: Any, schema: Mapping[str, Any], limit: int = 10) -> tuple[str, ...]:
    """Where ``value`` breaks ``schema``: each path with the constraint it breaks.

    Counts and the schema's own limits appear (``criteria[0].evidence: 4 items, at most 3``);
    the value's content never does, since it can hold user data.
    """
    validator = jsonschema.validators.validator_for(schema)(schema)
    errors = sorted(validator.iter_errors(value), key=lambda error: _path(error.absolute_path))
    return tuple(_finding(error) for error in errors[:limit])


def _path(parts: Iterable[Any]) -> str:
    path = ""
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else (f".{part}" if path else str(part))
    return path or "(root)"


def _finding(error: jsonschema.ValidationError) -> str:
    where = _path(error.absolute_path)
    kind, expected, instance = error.validator, error.validator_value, error.instance
    size = len(instance) if isinstance(instance, (list, str, dict)) else None
    described = {
        "maxItems": f"{size} items, at most {expected}",
        "minItems": f"{size} items, at least {expected}",
        "maxLength": f"{size} characters, at most {expected}",
        "minLength": f"{size} characters, at least {expected}",
        "maxProperties": f"{size} properties, at most {expected}",
        "minProperties": f"{size} properties, at least {expected}",
        "maximum": f"must be at most {expected}",
        "minimum": f"must be at least {expected}",
        "exclusiveMaximum": f"must be less than {expected}",
        "exclusiveMinimum": f"must be greater than {expected}",
        "multipleOf": f"must be a multiple of {expected}",
        "pattern": f"must match the pattern {expected}",
        "format": f"must be a valid {expected}",
        "uniqueItems": "items must be unique",
        "enum": "must be one of the values the schema allows",
        "const": "must be the value the schema requires",
    }
    if kind in described:
        return f"{where}: {described[kind]}"
    if kind == "type":
        types = expected if isinstance(expected, str) else " or ".join(expected)
        return f"{where}: must be of type {types}"
    if kind == "required" and isinstance(instance, dict):
        missing = ", ".join(name for name in expected if name not in instance)
        return f"{where}: missing required {missing}"
    if kind == "additionalProperties" and isinstance(instance, dict):
        allowed = error.schema.get("properties", {}) if isinstance(error.schema, dict) else {}
        extra = ", ".join(sorted(str(name) for name in instance if name not in allowed))
        return f"{where}: properties the schema does not allow: {extra}"
    return f"{where}: does not satisfy {kind}"


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

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._items))

    async def verify(
        self, names: Sequence[str], value: Any, context: Mapping[str, Any]
    ) -> tuple[VerificationResult, ...]:
        results = []
        for name in names:
            if name not in self._items:
                raise KeyError(f"verifier {name!r} is not registered")
            results.append(await self._items[name].verify(value, context))
        return tuple(results)
