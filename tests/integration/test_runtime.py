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
    history = await runtime.events.history(result.run_id)
    model_event = next(event for event in history if event.type == "model.completed")
    assert model_event.data["provider"] == "mock"
    assert model_event.data["model"] == "deterministic"
    assert model_event.data["cache_hit"] is False
    assert model_event.data["total_tokens"] == result.execution_summary.usage.total_tokens
    assert "latency_ms" in model_event.data
    assert "estimated_cost_usd" in model_event.data
    assert any(event.type == "policy.evaluated" for event in history)
    memory_event = next(event for event in history if event.type == "memory.written")
    assert memory_event.data == {
        "operation": "append",
        "item_count": 2,
        "retention_seconds": 86_400,
    }
    verification_event = next(event for event in history if event.type == "verification.completed")
    assert "message" not in verification_event.data
    assert "repair_hint" not in verification_event.data


async def test_started_run_can_be_waited_for_without_starting_a_second_run(runtime) -> None:
    state = await runtime.start(
        RunRequest(agent="test-agent", input="question", tenant_id="tenant", user_id="user")
    )

    result = await runtime.wait(state.id, "tenant")

    assert result.run_id == state.id
    assert result.status == RunStatus.COMPLETED
    history = await runtime.events.history(state.id)
    assert sum(event.type == "run.started" for event in history) == 1


async def test_tenant_cannot_read_another_run(runtime) -> None:
    state = await runtime.start(
        RunRequest(agent="test-agent", input="question", tenant_id="tenant-a", user_id="user")
    )
    assert await runtime.runs.get(state.id, "tenant-b") is None
