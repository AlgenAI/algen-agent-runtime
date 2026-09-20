from algen_agent_runtime.types.contracts import RunRequest, RunStatus


async def test_end_to_end_direct_run(runtime) -> None:
    result = await runtime.run(
        RunRequest(agent="test-agent", input="question", tenant_id="tenant", user_id="user")
    )
    assert result.status == RunStatus.COMPLETED
    assert result.output == "answer"
    assert result.execution_summary.model_calls == 1
    audit = await runtime.audits.list("tenant")
    assert any(event.action == "model.call" for event in audit)
    assert any(event.action == "policy.decision" for event in audit)


async def test_tenant_cannot_read_another_run(runtime) -> None:
    state = await runtime.start(
        RunRequest(agent="test-agent", input="question", tenant_id="tenant-a", user_id="user")
    )
    assert await runtime.runs.get(state.id, "tenant-b") is None
