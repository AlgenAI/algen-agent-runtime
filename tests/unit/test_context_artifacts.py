from __future__ import annotations

import json
from datetime import timedelta

import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.context.artifacts import (
    CONTEXT_READ_PERMISSION,
    CONTEXT_READ_TOOL,
    ContextArtifactService,
)
from algen_agent_runtime.exceptions.errors import (
    ConfigurationError,
    PolicyDeniedError,
    ToolExecutionError,
)
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.contracts import Tool, ToolContext, ToolDefinition
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.types.contracts import (
    Artifact,
    ArtifactStatus,
    ContextPolicy,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    RunRequest,
    RunState,
    utc_now,
)


def context() -> ToolContext:
    return ToolContext(
        run_id="run",
        step_id="step",
        tenant_id="tenant",
        user_id="user",
        permissions=frozenset({CONTEXT_READ_PERMISSION}),
        idempotency_key="run:call",
    )


def policy() -> ContextPolicy:
    return ContextPolicy(offload_tool_results=True, tool_result_max_inline_bytes=1024)


async def saved(service: ContextArtifactService, text: str = "x" * 5000) -> str:
    _, artifact_id = await service.offload(text, context(), policy(), started_at=utc_now())
    assert artifact_id is not None
    return artifact_id


async def test_unicode_paging_recovers_full_content_and_stable_reference() -> None:
    store = InMemoryArtifactStore()
    service = ContextArtifactService(store)
    text = json.dumps({"rows": "世界🌍" * 1800}, ensure_ascii=False)
    started = utc_now()
    reference, artifact_id = await service.offload(text, context(), policy(), started_at=started)
    assert artifact_id is not None
    repeated = await service.offload(text, context(), policy(), started_at=started)
    assert repeated == (reference, artifact_id)
    assert len(await store.list("tenant")) == 1
    assert len(reference.encode()) <= 1024
    assert json.loads(reference)["artifact_id"] == artifact_id
    reader = service.read_tool()
    pages = []
    offset = 0
    while True:
        page = await reader.execute(
            {"artifact_id": artifact_id, "offset": offset, "limit": 777}, context()
        )
        assert len(page["content"]) <= 777
        pages.append(page["content"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert "".join(pages) == text


@pytest.mark.parametrize("text", ["x" * 1024, "short"])
async def test_inline_threshold_does_not_create_artifacts(text: str) -> None:
    store = InMemoryArtifactStore()
    service = ContextArtifactService(store)
    assert await service.offload(text, context(), policy(), started_at=utc_now()) == (text, None)
    assert not await store.list("tenant")


async def test_disabled_policy_keeps_large_result_inline() -> None:
    store = InMemoryArtifactStore()
    service = ContextArtifactService(store)
    text = "x" * 20_000
    assert await service.offload(text, context(), ContextPolicy(), started_at=utc_now()) == (
        text,
        None,
    )
    assert not await store.list("tenant")


async def test_reader_does_not_accept_upload_with_spoofed_context_metadata() -> None:
    store = InMemoryArtifactStore()
    artifact = Artifact(
        tenant_id="tenant",
        run_id="run",
        name="upload",
        media_type="text/plain",
        data=b"upload",
        expires_at=utc_now() + timedelta(hours=1),
        metadata={
            "purpose": "context_tool_result",
            "context_user_id": "user",
            "context_execution_key": "run:call",
        },
    )
    await store.put(artifact)
    with pytest.raises(PolicyDeniedError):
        await (
            ContextArtifactService(store)
            .read_tool()
            .execute({"artifact_id": artifact.id}, context())
        )


async def test_preview_is_bounded_even_with_json_escapes() -> None:
    service = ContextArtifactService(InMemoryArtifactStore())
    reference, _ = await service.offload(
        "\x00" * 5000,
        context(),
        policy().model_copy(update={"tool_result_preview_chars": 1024}),
        started_at=utc_now(),
    )
    assert len(reference.encode()) <= 1024


@pytest.mark.parametrize(
    "updates",
    [
        {"tenant_id": "another"},
        {"run_id": "another"},
        {"user_id": "another"},
        {"permissions": frozenset()},
    ],
)
async def test_reader_enforces_identity_and_permission(updates: dict) -> None:
    service = ContextArtifactService(InMemoryArtifactStore())
    artifact_id = await saved(service)
    with pytest.raises(PolicyDeniedError):
        await service.read_tool().execute(
            {"artifact_id": artifact_id}, context().model_copy(update=updates)
        )


@pytest.mark.parametrize("status", [ArtifactStatus.QUARANTINED, ArtifactStatus.PENDING_SCAN])
async def test_lifecycle_change_blocks_reads_and_offload_reuse(status: ArtifactStatus) -> None:
    store = InMemoryArtifactStore()
    service = ContextArtifactService(store)
    artifact_id = await saved(service)
    await store.set_status(artifact_id, "tenant", status)
    with pytest.raises(PolicyDeniedError):
        await service.read_tool().execute({"artifact_id": artifact_id}, context())
    with pytest.raises(PolicyDeniedError):
        await saved(service)


async def test_expiry_is_not_extended_on_replay(monkeypatch) -> None:
    store = InMemoryArtifactStore()
    service = ContextArtifactService(store)
    started = utc_now()
    _, artifact_id = await service.offload("x" * 5000, context(), policy(), started_at=started)
    monkeypatch.setattr(
        "algen_agent_runtime.context.artifacts.utc_now", lambda: started + timedelta(days=2)
    )
    with pytest.raises(PolicyDeniedError):
        await service.read_tool().execute({"artifact_id": artifact_id}, context())
    with pytest.raises(ToolExecutionError, match="expired"):
        await service.offload("x" * 5000, context(), policy(), started_at=started)


@pytest.mark.parametrize(
    "bounds", [{"offset": -1}, {"offset": 6000}, {"limit": 0}, {"limit": 4097}]
)
async def test_page_bounds_are_enforced(bounds: dict) -> None:
    service = ContextArtifactService(InMemoryArtifactStore())
    artifact_id = await saved(service)
    with pytest.raises(ToolExecutionError):
        await service.read_tool().execute({"artifact_id": artifact_id, **bounds}, context())


async def test_new_read_checks_lifecycle_instead_of_using_result_cache() -> None:
    store = InMemoryArtifactStore()
    service = ContextArtifactService(store)
    artifact_id = await saved(service)
    registry = ToolRegistry()
    registry.register(service.read_tool())
    executor = ToolExecutor(registry, CompositePolicyEngine())
    assert (
        await executor.execute(CONTEXT_READ_TOOL, {"artifact_id": artifact_id}, context())
    ).value["content"]
    await store.delete(artifact_id, "tenant")
    # Completed-execution replay must also respect lifecycle changes.
    with pytest.raises(PolicyDeniedError):
        await executor.execute(CONTEXT_READ_TOOL, {"artifact_id": artifact_id}, context())
    with pytest.raises(PolicyDeniedError):
        await executor.execute(
            CONTEXT_READ_TOOL,
            {"artifact_id": artifact_id},
            context().model_copy(update={"idempotency_key": "new-read"}),
        )


async def test_completed_read_replay_cannot_cross_user_or_run() -> None:
    service = ContextArtifactService(InMemoryArtifactStore())
    artifact_id = await saved(service)
    registry = ToolRegistry()
    registry.register(service.read_tool())
    executor = ToolExecutor(registry, CompositePolicyEngine())
    await executor.execute(CONTEXT_READ_TOOL, {"artifact_id": artifact_id}, context())
    for updates in ({"user_id": "another"}, {"run_id": "another"}):
        with pytest.raises(PolicyDeniedError):
            await executor.execute(
                CONTEXT_READ_TOOL,
                {"artifact_id": artifact_id},
                context().model_copy(update=updates),
            )


def data_tool(value: str) -> Tool:
    return Tool(
        ToolDefinition(
            name="data.fetch",
            version="1",
            description="Fetch a report",
            input_schema={"type": "object"},
            output_schema={"type": "string"},
        ),
        lambda arguments, context: value,
    )


def agent():
    return make_agent(
        enabled_tools={"data.fetch", CONTEXT_READ_TOOL},
        tool_permissions={CONTEXT_READ_PERMISSION},
        context_policy=policy(),
    )


async def test_runtime_offloads_processed_result_and_reads_back_without_recursive_offload() -> None:
    store = InMemoryArtifactStore()
    source = "person@example.invalid " + "report row " * 500
    calls = 0

    def model(request: ModelRequest) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return tool_call_response("data.fetch", {})
        if calls == 2:
            reference = json.loads(request.messages[-1].text_content)
            assert reference["offloaded"]
            assert source not in request.messages[-1].text_content
            return tool_call_response(
                CONTEXT_READ_TOOL, {"artifact_id": reference["artifact_id"], "limit": 2000}
            )
        page = json.loads(request.messages[-1].text_content)
        assert len(page["content"]) == 2000
        assert "offloaded" not in page
        return ModelResponse(
            message=Message.text(Role.ASSISTANT, "done"), model="test", provider="mock"
        )

    runtime = make_runtime(
        MockModelProvider(handler=model), agent(), context_artifacts=ContextArtifactService(store)
    )
    runtime.tools.register(data_tool(source))
    result = await runtime.run(
        RunRequest(agent="test-agent", input="read report", tenant_id="tenant", user_id="user")
    )
    assert result.status == "completed", result.error
    assert len(result.artifacts) == 1
    artifact = await store.get(result.artifacts[0], "tenant")
    assert artifact is not None
    assert b"person@example.invalid" not in artifact.data
    assert b"REDACTED" in artifact.data
    state = await runtime.status(result.run_id, "tenant")
    restored = RunState.model_validate_json(state.model_dump_json())
    assert restored.context_artifact_ids == list(result.artifacts)
    audit = await runtime.audits.list("tenant")
    offload = next(item for item in audit if item.action == "context.offload")
    assert "preview" not in offload.metadata


async def test_storage_failure_keeps_completed_execution_and_never_reexecutes_tool() -> None:
    store = InMemoryArtifactStore(max_bytes=100)
    calls = 0
    tool = data_tool("large result " * 1000)
    original = tool.handler

    def counted(arguments, ctx):
        nonlocal calls
        calls += 1
        return original(arguments, ctx)

    tool.handler = counted
    provider = MockModelProvider([tool_call_response("data.fetch", {}), "must not run"])
    runtime = make_runtime(provider, agent(), context_artifacts=ContextArtifactService(store))
    runtime.tools.register(tool)
    result = await runtime.run(
        RunRequest(agent="test-agent", input="read report", tenant_id="tenant", user_id="user")
    )
    assert result.status == "failed"
    assert calls == 1
    assert len(provider.requests) == 1
    state = await runtime.status(result.run_id, "tenant")
    call = state.pending_tool_calls[0]
    replay_context = context().model_copy(
        update={
            "run_id": state.id,
            "step_id": f"tool-{call.id}",
            "idempotency_key": f"{state.id}:{call.id}",
        }
    )
    replay = await runtime.tool_executor.execute("data.fetch", {}, replay_context)
    assert replay.value == "large result " * 1000
    assert calls == 1


async def test_missing_reader_configuration_fails_before_dispatch() -> None:
    provider = MockModelProvider()
    runtime = make_runtime(provider, make_agent(context_policy=policy()))
    with pytest.raises(ConfigurationError, match="offloading requires"):
        await runtime.run(
            RunRequest(agent="test-agent", input="task", tenant_id="tenant", user_id="user")
        )
    assert not provider.requests


def test_container_wires_shared_artifact_store_and_reader() -> None:
    container = build_container(AppSettings())
    try:
        assert container.runtime.context_artifacts is not None
        assert container.runtime.context_artifacts.store is container.artifacts
        assert container.runtime.tools.get(CONTEXT_READ_TOOL).definition.required_permissions == {
            CONTEXT_READ_PERMISSION
        }
    finally:
        container.close()
