from __future__ import annotations

import pytest

from algen_agent_runtime.evaluation import ContextEvaluationArtifact, ContextEvaluationRunner
from examples.context_evaluation import FACT, cases, execute

pytestmark = pytest.mark.integration


async def test_offline_context_regression_gate_and_negative_control() -> None:
    artifacts = {case.id: await execute(case) for case in cases()}

    async def observed(case):
        return artifacts[case.id]

    report = await ContextEvaluationRunner().run(cases(), observed)
    assert report.promoted, report.model_dump_json()
    assert (
        artifacts["deep-archived"].checkpoint_bytes < artifacts["deep-inline"].checkpoint_bytes / 2
    )
    assert (
        artifacts["deep-archived"].recovered_messages == artifacts["deep-inline"].recovered_messages
    )
    # A missing summary fact must fail the same gate even when source archives remain intact.
    original = artifacts["deep-archived"]
    damaged = ContextEvaluationArtifact(
        messages=tuple(
            message.model_copy(
                update={
                    "content": tuple(
                        block.model_copy(update={"text": block.text.replace(FACT, "")})
                        for block in message.content
                    )
                }
            )
            for message in original.messages
        ),
        checkpoint_bytes=original.checkpoint_bytes,
        recovered_messages=original.recovered_messages,
    )
    result = ContextEvaluationRunner.evaluate(
        next(case for case in cases() if case.id == "deep-archived"), damaged
    )
    assert result.failures == ("required_text.0",)
