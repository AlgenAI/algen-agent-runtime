from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from algen_agent_runtime.evaluation import (
    ContextEvaluationArtifact,
    ContextEvaluationCase,
    ContextEvaluationRunner,
)
from algen_agent_runtime.types.contracts import Message, Role, ToolCall


def test_lost_fact_wrong_role_and_invented_claim_fail_without_report_content() -> None:
    case = ContextEvaluationCase(
        id="retention",
        required_messages=(Message.text(Role.USER, "Keep the original goal"),),
        required_text=("Warranty: eighteen months",),
        forbidden_text=("Lifetime warranty",),
    )
    result = ContextEvaluationRunner.evaluate(
        case,
        ContextEvaluationArtifact(
            messages=(Message.text(Role.ASSISTANT, "Keep the original goal. Lifetime warranty."),)
        ),
    )
    assert not result.passed
    assert set(result.failures) == {"required_message.0", "required_text.0", "forbidden_text.0"}
    assert "warranty" not in result.model_dump_json().lower()
    assert "original goal" not in result.model_dump_json()


def test_exact_messages_preserve_multiplicity() -> None:
    message = Message.text(Role.USER, "Repeat")
    case = ContextEvaluationCase(id="multiplicity", required_messages=(message, message))
    result = ContextEvaluationRunner.evaluate(case, ContextEvaluationArtifact(messages=(message,)))
    assert result.failures == ("required_message.1",)


@pytest.mark.parametrize("observed", [None, (), (Message.text(Role.USER, "Changed source"),)])
def test_recovery_requires_all_expected_sources_in_order(observed) -> None:
    source = (Message.text(Role.USER, "Original source"),)
    case = ContextEvaluationCase(id="recovery", expected_recovered_messages=source)
    result = ContextEvaluationRunner.evaluate(
        case, ContextEvaluationArtifact(messages=source, recovered_messages=observed)
    )
    assert result.failures == ("source_recovery",)


@pytest.mark.parametrize("checkpoint_bytes", [None, 101])
def test_budget_exceeded_and_missing_measurements_fail(checkpoint_bytes) -> None:
    case = ContextEvaluationCase(id="bounds", max_estimated_tokens=10, max_checkpoint_bytes=100)
    result = ContextEvaluationRunner.evaluate(
        case,
        ContextEvaluationArtifact(
            messages=(Message.text(Role.USER, "Large input " * 100),),
            checkpoint_bytes=checkpoint_bytes,
        ),
    )
    assert set(result.failures) == {"token_budget", "checkpoint_budget"}


@pytest.mark.parametrize("kind", ["orphan", "missing", "duplicate", "wrong_role", "complete"])
def test_broken_tool_exchanges_fail(kind) -> None:
    call = Message(
        role=Role.USER if kind == "wrong_role" else Role.ASSISTANT,
        content=(),
        tool_calls=(ToolCall(id="lookup-a", name="lookup"), ToolCall(id="lookup-b", name="lookup")),
    )
    first = Message(role=Role.TOOL, content=(), tool_call_id="lookup-a")
    second = Message(role=Role.TOOL, content=(), tool_call_id="lookup-b")
    messages = {
        "orphan": (first,),
        "missing": (call, first),
        "duplicate": (call, first, first),
        "wrong_role": (call, first, second),
        "complete": (call, second, first),
    }[kind]
    result = ContextEvaluationRunner.evaluate(
        ContextEvaluationCase(id="protocol", max_estimated_tokens=1000),
        ContextEvaluationArtifact(messages=messages),
    )
    assert result.failures == (() if kind == "complete" else ("tool_protocol",))


async def test_failure_cannot_be_hidden_by_promotion_threshold() -> None:
    case = ContextEvaluationCase(id="fact", required_text=("expected",))

    async def execute(_):
        return ContextEvaluationArtifact(messages=(Message.text(Role.ASSISTANT, "different"),))

    report = await ContextEvaluationRunner().run((case,), execute, promotion_threshold=0)
    assert report.score > 0
    assert not report.promoted


async def test_execution_failure_is_content_free_and_does_not_stop_other_cases() -> None:
    cases = tuple(
        ContextEvaluationCase(id=name, max_estimated_tokens=100) for name in ("fails", "works")
    )

    async def execute(case):
        if case.id == "fails":
            raise RuntimeError("private provider payload")
        return ContextEvaluationArtifact(messages=(Message.text(Role.USER, "Hello"),))

    report = await ContextEvaluationRunner().run(cases, execute)
    assert report.cases[0].failures == ("execution",)
    assert report.cases[1].passed
    assert "private" not in report.model_dump_json()
    assert not report.promoted


async def test_cancellation_propagates() -> None:
    async def execute(_):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await ContextEvaluationRunner().run(
            (ContextEvaluationCase(id="cancel", max_estimated_tokens=100),), execute
        )


@pytest.mark.parametrize("threshold", [-0.1, 1.1, float("nan")])
async def test_invalid_threshold_is_rejected_before_executor(threshold) -> None:
    async def execute(_):
        pytest.fail("must not execute")

    with pytest.raises(ValueError, match="threshold"):
        await ContextEvaluationRunner().run((), execute, promotion_threshold=threshold)


async def test_duplicate_cases_rejected_and_empty_report_does_not_promote() -> None:
    async def execute(_):
        pytest.fail("must not execute")

    case = ContextEvaluationCase(id="duplicate", max_estimated_tokens=100)
    with pytest.raises(ValueError, match="unique"):
        await ContextEvaluationRunner().run((case, case), execute)
    assert not (await ContextEvaluationRunner().run((), execute)).promoted


@pytest.mark.parametrize(
    "updates",
    [{}, {"required_text": (" ",)}, {"id": "prompt content", "max_estimated_tokens": 100}],
)
def test_cases_require_valid_id_and_nonempty_expectations(updates) -> None:
    with pytest.raises(ValidationError):
        ContextEvaluationCase(**{"id": "invalid", **updates})
