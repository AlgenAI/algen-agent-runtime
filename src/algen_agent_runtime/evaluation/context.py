from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from algen_agent_runtime.context.packing import estimate_message, message_groups
from algen_agent_runtime.evaluation.contracts import CaseEvaluation, EvaluationReport
from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.types.contracts import Message, Role


class ContextEvaluationCase(BaseModel):
    """Explicit expectations for a context view; text checks are lexical, not semantic."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    required_messages: tuple[Message, ...] = ()
    required_text: tuple[str, ...] = ()
    forbidden_text: tuple[str, ...] = ()
    max_estimated_tokens: int | None = Field(default=None, ge=1)
    max_checkpoint_bytes: int | None = Field(default=None, ge=1)
    expected_recovered_messages: tuple[Message, ...] | None = None

    @model_validator(mode="after")
    def meaningful_expectations(self) -> ContextEvaluationCase:
        if any(not text.strip() for text in (*self.required_text, *self.forbidden_text)):
            raise ValueError("text expectations must not be blank")
        if not (
            self.required_messages
            or self.required_text
            or self.forbidden_text
            or self.max_estimated_tokens is not None
            or self.max_checkpoint_bytes is not None
            or self.expected_recovered_messages is not None
        ):
            raise ValueError("a context case requires at least one expectation")
        return self


class ContextEvaluationArtifact(BaseModel):
    """Host-supplied observations. Reports never include these source payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    messages: tuple[Message, ...]
    checkpoint_bytes: int | None = Field(default=None, ge=0)
    recovered_messages: tuple[Message, ...] | None = None


ContextCaseExecutor = Callable[[ContextEvaluationCase], Awaitable[ContextEvaluationArtifact]]


class ContextEvaluationRunner:
    """Deterministic checks usable with scripted or deployment-supplied executors."""

    async def run(
        self,
        cases: Sequence[ContextEvaluationCase],
        executor: ContextCaseExecutor,
        *,
        promotion_threshold: float = 1.0,
    ) -> EvaluationReport:
        if not math.isfinite(promotion_threshold) or not 0 <= promotion_threshold <= 1:
            raise ValueError("promotion_threshold must be between zero and one")
        if len({case.id for case in cases}) != len(cases):
            raise ValueError("context case IDs must be unique")
        results: list[CaseEvaluation] = []
        for case in cases:
            try:
                artifact = await executor(case)
                result = self.evaluate(case, artifact)
            except Exception:
                # Failure details can contain prompts, secrets, or provider payloads.
                # Cancellation (BaseException) still propagates to the caller.
                result = CaseEvaluation(
                    case_id=case.id,
                    score=0,
                    passed=False,
                    checks={"execution": False},
                    failures=("execution",),
                )
            results.append(result)
        score = sum(result.score for result in results) / len(results) if results else 0
        return EvaluationReport(
            cases=tuple(results),
            score=score,
            promotion_threshold=promotion_threshold,
            promoted=bool(results)
            and score >= promotion_threshold
            and all(result.passed for result in results),
        )

    @staticmethod
    def evaluate(
        case: ContextEvaluationCase, artifact: ContextEvaluationArtifact
    ) -> CaseEvaluation:
        checks: dict[str, bool] = {}
        # Count each expected message, preserving multiplicity as well as exact content/role.
        remaining = list(artifact.messages)
        for index, message in enumerate(case.required_messages):
            found = message in remaining
            checks[f"required_message.{index}"] = found
            if found:
                remaining.remove(message)
        text = "\n".join(message.text_content for message in artifact.messages).casefold()
        for index, required in enumerate(case.required_text):
            checks[f"required_text.{index}"] = required.casefold() in text
        for index, forbidden in enumerate(case.forbidden_text):
            checks[f"forbidden_text.{index}"] = forbidden.casefold() not in text
        if case.max_estimated_tokens is not None:
            checks["token_budget"] = (
                sum(estimate_message(message) for message in artifact.messages)
                <= case.max_estimated_tokens
            )
        if case.max_checkpoint_bytes is not None:
            checks["checkpoint_budget"] = (
                artifact.checkpoint_bytes is not None
                and artifact.checkpoint_bytes <= case.max_checkpoint_bytes
            )
        if case.expected_recovered_messages is not None:
            checks["source_recovery"] = (
                artifact.recovered_messages == case.expected_recovered_messages
            )
        try:
            groups = message_groups(artifact.messages)
            checks["tool_protocol"] = all(
                not group[0].tool_calls
                or (
                    group[0].role == Role.ASSISTANT
                    and len({call.id for call in group[0].tool_calls}) == len(group[0].tool_calls)
                    and len(group) - 1 == len(group[0].tool_calls)
                    and {message.tool_call_id for message in group[1:]}
                    == {call.id for call in group[0].tool_calls}
                )
                for group in groups
            )
        except ConflictError:
            checks["tool_protocol"] = False
        failures = tuple(name for name, passed in checks.items() if not passed)
        return CaseEvaluation(
            case_id=case.id,
            checks=checks,
            failures=failures,
            passed=not failures,
            score=sum(checks.values()) / len(checks),
        )
