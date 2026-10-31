"""Attachments over HTTP, through workflows and conversations, to tools, and from the CLI (#82)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.artifacts import BasicArtifactScanner
from algen_agent_runtime.cli import main
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.tools.contracts import Tool, ToolContext, ToolDefinition
from algen_agent_runtime.types.contracts import (
    Artifact,
    ArtifactStatus,
    FileBlock,
    RunAttachment,
)
from algen_agent_runtime.workflows import MultiAgentWorkflowExecutor, WorkflowHookRegistry
from algen_agent_runtime.workflows.contracts import (
    WorkflowExecutionState,
    WorkflowManifest,
    WorkflowNode,
    WorkflowNodeKind,
)

pytestmark = pytest.mark.anyio

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x02\x00\x00\x00\x02\x08\x02\x00\x00\x00"
    b"\xfd\xd4\x9as\x00\x00\x00\x00IEND\xaeB`\x82"
)
HEADERS = {
    "x-tenant-id": "acme",
    "x-user-id": "clerk",
    "x-scopes": "runs:read runs:write conversations:read conversations:write",
}


def _settings(**agent: Any) -> AppSettings:
    return AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
            "agents": [
                {
                    "name": "reader",
                    "version": "1.0.0",
                    "description": "Reads documents",
                    "system_instructions": "Say what the document is.",
                    "default_model": {"name": "d", "provider": "mock", "model": "deterministic"},
                    "attachments": {"enabled": True},
                    **agent,
                }
            ],
            "security": {"upload_scanner": "basic"},
            "runtime": {"recover_incomplete_runs": False},
        }
    )


def _mock(app: Any) -> MockModelProvider:
    return next(
        provider
        for provider in app.state.container.runtime.router.providers()
        if isinstance(provider, MockModelProvider)
    )


async def _upload(client: httpx.AsyncClient, data: bytes, name: str, media_type: str) -> Any:
    response = await client.post(
        f"/v1/artifacts?name={name}&media_type={media_type.replace('/', '%2F')}",
        headers=HEADERS,
        content=data,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_an_upload_is_scanned_and_a_run_reads_it() -> None:
    app = create_app(_settings())
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        image = await _upload(client, PNG, "passport.png", "image/png")
        fake = await _upload(client, b"MZ\x90\x00", "photo.png", "image/png")
        assert image["status"] == "available"
        assert fake["status"] == "quarantined"

        started = await client.post(
            "/v1/runs",
            headers=HEADERS,
            json={
                "agent": "reader",
                "input": "What is this?",
                "attachments": [{"artifact_id": image["id"]}],
            },
        )
        assert started.status_code == 202, started.text
        run_id = started.json()["run_id"]
        for _ in range(300):
            run = (await client.get(f"/v1/runs/{run_id}", headers=HEADERS)).json()
            if run["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(0.01)
        assert run["status"] == "completed", run.get("error")
        assert run["request"]["attachments"] == [
            {"artifact_id": image["id"], "media_type": "image/png"}
        ]

    [sent] = _mock(app).requests
    files = [b for m in sent.messages for b in m.content if isinstance(b, FileBlock)]
    assert [f.content for f in files] == [PNG]


async def test_a_conversation_message_hands_its_files_to_the_turn() -> None:
    app = create_app(_settings())
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        image = await _upload(client, PNG, "receipt.png", "image/png")
        conversation = (
            await client.post("/v1/conversations", headers=HEADERS, json={"agent": "reader"})
        ).json()
        accepted = await client.post(
            f"/v1/conversations/{conversation['id']}/messages",
            headers=HEADERS,
            json={"text": "Read this receipt", "attachments": [{"artifact_id": image["id"]}]},
        )
        assert accepted.status_code == 202, accepted.text
        for _ in range(300):
            messages = (
                await client.get(
                    f"/v1/conversations/{conversation['id']}/messages", headers=HEADERS
                )
            ).json()
            if messages[-1]["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        assert messages[0]["content"][1] == {
            "type": "file",
            "artifact_id": image["id"],
            "media_type": None,
        }
    sent = _mock(app).requests[-1]
    assert any(
        isinstance(b, FileBlock) and b.content == PNG for m in sent.messages for b in m.content
    )


async def test_a_workflow_that_does_not_accept_files_refuses_them() -> None:
    settings = _settings()
    settings = settings.model_copy(
        update={
            "workflows": {
                "plain": WorkflowManifest(
                    name="plain",
                    version="1.0.0",
                    nodes=(
                        WorkflowNode(
                            id="read",
                            kind=WorkflowNodeKind.AGENT,
                            agent="reader",
                            input_template="{{input}}",
                            output_key="out",
                        ),
                    ),
                )
            }
        }
    )
    app = create_app(settings)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        refused = await client.post(
            "/v1/workflows/plain/runs",
            headers=HEADERS,
            json={"input": "x", "attachments": [{"artifact_id": "a-1"}]},
        )
        assert refused.status_code >= 400
        assert "does not accept attachments" in refused.text
        listed = (await client.get("/v1/workflows", headers=HEADERS)).json()["workflows"]
        assert listed[0]["attachments"]["enabled"] is False


class _Runner:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def run(self, request: Any) -> Any:
        from types import SimpleNamespace

        from algen_agent_runtime.types.contracts import RunStatus

        self.requests.append(request)
        return SimpleNamespace(
            run_id=f"r{len(self.requests)}", status=RunStatus.COMPLETED, output="ok", error=None
        )


async def test_only_nodes_that_pass_attachments_hand_them_to_their_agent() -> None:
    manifest = WorkflowManifest(
        name="kyc",
        version="1.0.0",
        attachments={"enabled": True},
        nodes=(
            WorkflowNode(
                id="classify",
                kind=WorkflowNodeKind.AGENT,
                agent="reader",
                input_template="{{input}}",
                output_key="kind",
                pass_attachments=True,
            ),
            WorkflowNode(
                id="summarise",
                kind=WorkflowNodeKind.AGENT,
                agent="writer",
                input_template="{{kind}}",
                output_key="summary",
                depends_on=("classify",),
            ),
        ),
    )
    runner = _Runner()
    files = (RunAttachment(artifact_id="a-1", media_type="image/png"),)

    state = await MultiAgentWorkflowExecutor(runner, WorkflowHookRegistry()).run(
        manifest, {"input": "check"}, tenant_id="t", user_id="u", attachments=files
    )

    assert state.status.value == "completed"
    assert [request.attachments for request in runner.requests] == [files, ()]
    assert "attachments" in state.model_dump()
    assert (
        "attachments"
        not in WorkflowExecutionState(
            id="x", manifest_name="m", manifest_version="1", tenant_id="t", user_id="u"
        ).model_dump()
    )


def test_a_manifest_without_attachments_keeps_its_earlier_dump() -> None:
    node = WorkflowNode(
        id="a", kind=WorkflowNodeKind.AGENT, agent="x", input_template="{{input}}", output_key="o"
    )
    dumped = WorkflowManifest(name="m", version="1", nodes=(node,)).model_dump()
    assert "attachments" not in dumped
    assert "pass_attachments" not in dumped["nodes"][0]


def test_a_node_cannot_pass_files_to_an_agent_that_refuses_them() -> None:
    with pytest.raises(ValueError, match="does not accept them"):
        _settings(attachments={"enabled": False}).model_copy().model_validate(
            {
                **_settings(attachments={"enabled": False}).model_dump(mode="json"),
                "workflows": {
                    "kyc": {
                        "name": "kyc",
                        "version": "1.0.0",
                        "attachments": {"enabled": True},
                        "nodes": [
                            {
                                "id": "read",
                                "kind": "agent",
                                "agent": "reader",
                                "input_template": "{{input}}",
                                "output_key": "o",
                                "pass_attachments": True,
                            }
                        ],
                    }
                },
            }
        )


async def test_a_tool_reads_the_run_attachments_and_nothing_else() -> None:
    from conftest import make_agent, make_runtime

    from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
    from algen_agent_runtime.types.contracts import (
        AttachmentPolicy,
        FinishReason,
        Message,
        ModelResponse,
        Role,
        RunRequest,
        ToolCall,
    )

    store = InMemoryArtifactStore()
    attached = Artifact(tenant_id="acme", name="a.png", media_type="image/png", data=PNG)
    other = Artifact(tenant_id="acme", name="b.png", media_type="image/png", data=PNG)
    await store.put(attached)
    await store.put(other)
    read: list[Any] = []

    async def extract(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        read.append((await context.read_attachment(attached.id)).name)
        try:
            await context.read_attachment(other.id)
        except Exception as exc:
            read.append(type(exc).__name__)
        return {"ok": True}

    call = ModelResponse(
        message=Message.text(Role.ASSISTANT, ""),
        tool_calls=(ToolCall(name="docs.extract", arguments={}),),
        finish_reason=FinishReason.TOOL_CALLS,
        model="deterministic",
        provider="mock",
    )
    runtime = make_runtime(
        MockModelProvider([call, "done"]),
        make_agent(
            enabled_tools=frozenset({"docs.extract"}), attachments=AttachmentPolicy(enabled=True)
        ),
    )
    runtime.artifacts = store
    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="docs.extract",
                version="1",
                description="Extract text",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
            ),
            extract,
        )
    )

    await runtime.run(
        RunRequest(
            agent="test-agent",
            input="go",
            tenant_id="acme",
            user_id="u",
            attachments=(RunAttachment(artifact_id=attached.id),),
        )
    )

    assert read == ["a.png", "PolicyDeniedError"]


@pytest.mark.parametrize(
    ("data", "declared", "status", "reason"),
    [
        (PNG, "image/png", ArtifactStatus.AVAILABLE, "clean"),
        (PNG, "application/octet-stream", ArtifactStatus.AVAILABLE, "clean"),
        (PNG, "image/jpeg", ArtifactStatus.QUARANTINED, "type_mismatch"),
        (b"plain text", "image/png", ArtifactStatus.QUARANTINED, "type_unrecognised"),
        (b"a,b\n1,2\n", "text/csv", ArtifactStatus.AVAILABLE, "not_inspected"),
        (
            b"%PDF-1.4\nno end",
            "application/pdf",
            ArtifactStatus.QUARANTINED,
            "malformed_or_too_large",
        ),
    ],
)
async def test_the_basic_scanner(
    data: bytes, declared: str, status: ArtifactStatus, reason: str
) -> None:
    result = await BasicArtifactScanner().scan(
        Artifact(tenant_id="t", name="f", media_type=declared, data=data)
    )
    assert (result.status, result.reason_code) == (status, reason)


def test_the_cli_runs_an_agent_with_a_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "id.png").write_bytes(PNG)
    config = tmp_path / "agent.yaml"
    config.write_text(
        "providers:\n  mock: {type: mock, default_model: deterministic}\n"
        "runtime: {recover_incomplete_runs: false}\n"
        "agents:\n"
        "  - name: reader\n    version: 1.0.0\n    description: d\n"
        "    system_instructions: x\n"
        "    default_model: {name: d, provider: mock, model: deterministic}\n"
        "    attachments: {enabled: true}\n"
    )

    with pytest.raises(SystemExit) as exited:
        main(
            [
                "run",
                "reader",
                "What is this?",
                "--attach",
                str(tmp_path / "id.png"),
                "--config",
                str(config),
            ]
        )

    assert exited.value.code == 0, capsys.readouterr().err
    assert capsys.readouterr().out.strip()
