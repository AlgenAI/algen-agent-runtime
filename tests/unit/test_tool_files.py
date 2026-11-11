"""Files a tool hands to the model (#129): checked like run attachments, tenant included, shown
as file blocks on the tool's message when a model reads them, and held by reference only."""

from __future__ import annotations

import struct
import zlib
from typing import Any

import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.providers.anthropic import AnthropicProvider
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.models.providers.openai_compatible import OpenAICompatibleProvider
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.runtime.attachments import DATA_NOTE, AttachmentLimits, tool_files
from algen_agent_runtime.tools.authoring import tool
from algen_agent_runtime.tools.contracts import (
    Tool,
    ToolContext,
    ToolDefinition,
    ToolFile,
    ToolResult,
)
from algen_agent_runtime.types.contracts import (
    Artifact,
    Budget,
    FileBlock,
    Message,
    ModelCapabilities,
    ModelRequest,
    Role,
    RunRequest,
    RunStatus,
    TextBlock,
    ToolCall,
)

TENANT = "tenant-a"


def png(width: int = 2, height: int = 2) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + header
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(header))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
        + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _page(data: bytes | None = None, **values: Any) -> Artifact:
    return Artifact(
        tenant_id=values.pop("tenant_id", TENANT),
        name="page-1.png",
        media_type="image/png",
        data=png() if data is None else data,
        **values,
    )


def _tool(*files: ToolFile, returns_files: bool = True) -> Tool:
    async def render(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(value={"pages": 1, "coverage": "none"}, files=files)

    return Tool(
        ToolDefinition(
            name="docs.render",
            version="1",
            description="Render a document's pages",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            returns_files=returns_files,
        ),
        render,
    )


async def _run(
    *artifacts: Artifact,
    files: tuple[ToolFile, ...] | None = None,
    provider: MockModelProvider | None = None,
    returns_files: bool = True,
    **agent: Any,
) -> tuple[Any, Any, MockModelProvider]:
    store = InMemoryArtifactStore()
    for artifact in artifacts:
        await store.put(artifact)
    provider = provider or MockModelProvider()
    provider.queue(tool_call_response("docs.render", {}), "It is a passport.")
    runtime = make_runtime(provider, make_agent(enabled_tools=frozenset({"docs.render"}), **agent))
    if isinstance(provider, _TextOnly):
        runtime.router = ModelRouter()
        runtime.router.register_provider(provider, registration_id="mock")
    runtime.artifacts = store
    if files is None:
        files = tuple(ToolFile(artifact_id=a.id, media_type="image/png") for a in artifacts)
    runtime.tools.register(_tool(*files, returns_files=returns_files))
    result = await runtime.run(
        RunRequest(agent="test-agent", input="Read the scan.", tenant_id=TENANT, user_id="u")
    )
    return runtime, result, provider


def _tool_message(request: ModelRequest) -> Message:
    return next(message for message in request.messages if message.role == Role.TOOL)


class _TextOnly(MockModelProvider):
    async def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(tools=True, images=False, files=False)


async def test_a_tool_shows_the_model_an_image_and_the_run_keeps_only_its_reference() -> None:
    page = _page()
    runtime, result, provider = await _run(page)

    assert result.status == RunStatus.COMPLETED, result.error
    tool_message = _tool_message(provider.requests[1])
    [file] = [block for block in tool_message.content if isinstance(block, FileBlock)]
    assert file.content == page.data and file.media_type == "image/png"
    assert any(isinstance(b, TextBlock) and b.text == DATA_NOTE for b in tool_message.content)
    state = await runtime.runs.get(result.run_id, TENANT)
    stored = state.model_dump_json()
    assert page.id in stored and "iVBORw0KGgo" not in stored and "\\x89PNG" not in stored
    events = await runtime.events.history(result.run_id)
    [completed] = [event for event in events if event.type == "tool.completed"]
    assert completed.data["file_count"] == 1 and completed.data["files_refused"] == 0


async def test_a_text_only_model_sees_the_placeholder() -> None:
    page = _page()
    _, result, provider = await _run(page, provider=_TextOnly())

    assert result.status == RunStatus.COMPLETED, result.error
    tool_message = _tool_message(provider.requests[1])
    assert not any(isinstance(block, FileBlock) for block in tool_message.content)
    assert f"[attachment: page-1.png, image/png, id={page.id}]" in tool_message.text_content


@pytest.mark.parametrize(
    ("artifact", "declared", "returns_files", "reason"),
    [
        (_page(tenant_id="tenant-b"), "image/png", True, "was not found"),
        (_page(png(9000, 10)), "image/png", True, "at most 8000 per side"),
        (_page(b"\x89PNG\r\n\x1a\nbroken"), "image/png", True, "not a well-formed image"),
        (_page(b"just text"), None, True, "is text/plain; a model reads only"),
        (_page(), "image/jpeg", True, "declared image/jpeg but is image/png"),
        (_page(), "image/png", False, "does not declare returns_files"),
    ],
)
async def test_a_file_the_model_cannot_take_is_refused_with_a_note(
    artifact: Artifact, declared: str | None, returns_files: bool, reason: str
) -> None:
    files = (ToolFile(artifact_id=artifact.id, media_type=declared, name="page 1"),)
    runtime, result, provider = await _run(artifact, files=files, returns_files=returns_files)

    assert result.status == RunStatus.COMPLETED, result.error
    tool_message = _tool_message(provider.requests[1])
    assert not any(isinstance(block, FileBlock) for block in tool_message.content)
    assert "[file page 1 from the tool was refused: " in tool_message.text_content
    assert reason in tool_message.text_content
    events = await runtime.events.history(result.run_id)
    [completed] = [event for event in events if event.type == "tool.completed"]
    assert completed.data["files_refused"] == 1


async def test_files_count_against_the_token_budget() -> None:
    page = _page()
    _, result, provider = await _run(page, budget=Budget(max_tokens=1_000))

    tool_message = _tool_message(provider.requests[1])
    assert "remaining token budget" in tool_message.text_content
    assert result.status == RunStatus.COMPLETED, result.error


async def test_limits_bound_the_files_of_one_result() -> None:
    store = InMemoryArtifactStore()
    pages = [_page() for _ in range(3)]
    for page in pages:
        await store.put(page)

    async def readable(needed: frozenset[str]) -> bool:
        return True

    checked = await tool_files(
        [ToolFile(artifact_id=page.id) for page in pages],
        declared=True,
        tenant_id=TENANT,
        artifacts=store,
        limits=AttachmentLimits(max_count=2),
        remaining_tokens=None,
        readable=readable,
    )

    assert (checked.shown, checked.refused) == (2, 1)
    assert "at most 2 files" in checked.blocks[-1].text  # type: ignore[union-attr]


async def test_an_authored_tool_returns_files_through_a_tool_result() -> None:
    page = _page()

    @tool(returns_files=True)
    async def render_pages(document_id: str) -> ToolResult:
        """Render a document's pages."""
        return ToolResult(
            value={"pages": 1}, files=(ToolFile(artifact_id=page.id, media_type="image/png"),)
        )

    assert render_pages.definition.returns_files is True
    assert render_pages.definition.output_schema == {}
    context = ToolContext(
        run_id="r",
        step_id="s",
        tenant_id=TENANT,
        user_id="u",
        permissions=frozenset(),
        idempotency_key="k",
    )
    returned = await render_pages.handler({"document_id": "d"}, context)

    assert isinstance(returned, ToolResult)
    assert returned.value == {"pages": 1} and returned.files[0].artifact_id == page.id


def test_a_tool_result_without_files_serializes_as_before() -> None:
    assert "files" not in ToolResult(value=1).model_dump()
    assert ToolResult(value=1, files=(ToolFile(artifact_id="a"),)).model_dump()["files"]
    assert (
        ToolDefinition(
            name="t.x", version="1", description="d", input_schema={}, output_schema={}
        ).returns_files
        is False
    )


def _resolved_tool_messages(*call_ids: str) -> ModelRequest:
    data = png()
    calls = tuple(ToolCall(id=call_id, name="docs.render") for call_id in call_ids)
    results = tuple(
        Message(
            role=Role.TOOL,
            name="docs.render",
            tool_call_id=call_id,
            content=(
                TextBlock(text='{"pages": 1}'),
                FileBlock(artifact_id=f"a-{call_id}").with_content(
                    data, name="page-1.png", media_type="image/png"
                ),
            ),
        )
        for call_id in call_ids
    )
    return ModelRequest(
        messages=(
            Message.text(Role.USER, "Read the scan."),
            Message(role=Role.ASSISTANT, content=(), tool_calls=calls),
            *results,
        )
    )


def test_anthropic_puts_the_image_inside_the_tool_result() -> None:
    provider = AnthropicProvider("env://ANTHROPIC_API_KEY", "claude-sonnet-5-5")

    payload = provider._payload(_resolved_tool_messages("c1", "c2"))

    [turn] = [
        m for m in payload["messages"] if m["role"] == "user" and m is not payload["messages"][0]
    ]
    first = turn["content"][0]
    assert first["type"] == "tool_result" and first["tool_use_id"] == "c1"
    assert first["content"][0] == {"type": "text", "text": '{"pages": 1}'}
    assert first["content"][1]["type"] == "image"
    assert first["content"][1]["source"]["media_type"] == "image/png"
    assert [item["tool_use_id"] for item in turn["content"]] == ["c1", "c2"]


def test_openai_compatible_apis_get_the_files_after_the_tool_results() -> None:
    provider = OpenAICompatibleProvider(
        "compat",
        "https://compat.example.test/v1",
        None,
        "m",
        ModelCapabilities(tools=True, images=True),
    )

    payload = provider._payload(_resolved_tool_messages("c1", "c2"))

    roles = [message["role"] for message in payload["messages"]]
    assert roles == ["user", "assistant", "tool", "tool", "user"]
    for tool_message in payload["messages"][2:4]:
        texts = tool_message["content"]
        assert all(part["type"] == "text" for part in texts)
        assert any("[attachment: page-1.png, image/png" in part["text"] for part in texts)
    files = payload["messages"][-1]["content"]
    assert [part["type"] for part in files] == ["text", "image_url", "text", "image_url"]
    assert files[0]["text"] == "Files returned by tool call c1:"
    assert files[1]["image_url"]["url"].startswith("data:image/png;base64,")
