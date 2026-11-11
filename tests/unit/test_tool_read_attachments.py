"""Files a tool reads (#130): Office documents, text and e-mail ride along with a run, never reach
a provider, and reach a reader tool by the id in their placeholder."""

from __future__ import annotations

import io
import re
import struct
import zipfile
from typing import Any

import pytest
from conftest import make_agent, make_runtime
from pydantic import ValidationError

from algen_agent_runtime.artifacts import BasicArtifactScanner
from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.runtime.attachments import (
    AttachmentLimits,
    accepted_media_type,
    required_capabilities,
    sniff_media_type,
)
from algen_agent_runtime.tools.contracts import Tool, ToolContext, ToolDefinition
from algen_agent_runtime.types.contracts import (
    MODEL_ATTACHMENT_TYPES,
    Artifact,
    ArtifactStatus,
    AttachmentPolicy,
    FileBlock,
    FinishReason,
    Message,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    Role,
    RunAttachment,
    RunRequest,
    RunStatus,
)

TENANT = "tenant-a"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def ooxml(part: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(part, "<document/>")
    return buffer.getvalue()


def ole(*streams: str) -> bytes:
    """A compound file's header and a directory sector naming these streams."""
    header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1".ljust(512, b"\x00")
    entries = b""
    for index, name in enumerate(("Root Entry", *streams)):
        encoded = (name + "\x00").encode("utf-16-le")
        entry = encoded.ljust(64, b"\x00") + struct.pack("<HB", len(encoded), 5 if not index else 2)
        entries += entry.ljust(128, b"\x00")
    return header + entries.ljust(512, b"\x00")


EML = (
    b"From: Ana <ana@example.com>\r\nTo: hr@example.com\r\nSubject: Leave request\r\n"
    b"Date: Tue, 10 Nov 2026 09:00:00 +0000\r\n\r\nI would like next Friday off.\r\n"
)
CSV = b"name,days\nAna,3\n"


async def _store(*artifacts: Artifact) -> InMemoryArtifactStore:
    store = InMemoryArtifactStore()
    for artifact in artifacts:
        await store.put(artifact)
    return store


def _artifact(data: bytes, name: str, media_type: str, **values: Any) -> Artifact:
    return Artifact(
        tenant_id=values.pop("tenant_id", TENANT),
        name=name,
        media_type=media_type,
        data=data,
        **values,
    )


def _policy(*types: str) -> AttachmentPolicy:
    return AttachmentPolicy(enabled=True, tool_read_types=frozenset(types or {DOCX}))


def _runtime(store: InMemoryArtifactStore, provider: Any = None, **agent: Any) -> Any:
    agent.setdefault("attachments", _policy())
    runtime = make_runtime(provider or MockModelProvider(["Done."]), make_agent(**agent))
    runtime.artifacts = store
    return runtime


def _request(*attachments: RunAttachment, **values: Any) -> RunRequest:
    return RunRequest(
        agent="test-agent",
        input="Summarise the attached file.",
        tenant_id=values.pop("tenant_id", TENANT),
        user_id="u",
        attachments=attachments,
    )


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (ooxml("word/document.xml"), DOCX),
        (ooxml("xl/workbook.xml"), XLSX),
        (ooxml("ppt/presentation.xml"), PPTX),
        (ole("WordDocument"), "application/msword"),
        (ole("Workbook"), "application/vnd.ms-excel"),
        (ole("PowerPoint Document"), "application/vnd.ms-powerpoint"),
        (ole("__properties_version1.0", "WordDocument"), "application/vnd.ms-outlook"),
        (EML, "message/rfc822"),
        (CSV, "text/plain"),
        ("Grüße, ça va\n".encode(), "text/plain"),
        # Neither an Office document nor text.
        (ole("Something"), None),
        (b"PK\x03\x04 not really a zip", None),
        (ooxml("other/part.xml"), None),
        (b"MZ\x90\x00\x03\x00\x00\x00", None),
        (b"\x01\x02\x03 mostly control bytes", None),
        (b"", None),
    ],
)
def test_office_documents_email_and_text_are_recognised_from_their_bytes(
    data: bytes, expected: str | None
) -> None:
    assert sniff_media_type(data) == expected


def test_yaml_with_a_from_key_is_not_email() -> None:
    assert sniff_media_type(b"from: a\nto: b\n\nrest") == "text/plain"
    assert sniff_media_type(b"From: a\nkey value\nDate: x\nTo: y\n") == "text/plain"


def test_a_declared_text_type_names_text_and_other_types_must_match() -> None:
    assert accepted_media_type("text/plain", "text/csv") == "text/csv"
    assert accepted_media_type("text/plain", "text/csv; charset=utf-8") == "text/csv"
    assert accepted_media_type("message/rfc822", "text/plain") == "text/plain"
    assert accepted_media_type("text/plain", None) == "text/plain"
    assert accepted_media_type(DOCX, DOCX) == DOCX
    assert accepted_media_type(XLSX, DOCX) is None
    assert accepted_media_type("text/plain", DOCX) is None
    assert accepted_media_type("text/plain", "image/png") is None


async def test_a_docx_reaches_a_reader_tool_and_never_a_model() -> None:
    document = _artifact(ooxml("word/document.xml"), "contract.docx", DOCX)
    read: list[tuple[str, bytes]] = []

    async def extract(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        artifact = await context.read_attachment(arguments["artifact_id"])
        read.append((artifact.name, artifact.data))
        return {"text": "The contract runs for a year."}

    def reply(request: ModelRequest) -> ModelResponse:
        if read:
            return ModelResponse(
                message=Message.text(Role.ASSISTANT, "It runs for a year."),
                finish_reason=FinishReason.STOP,
                model="deterministic",
                provider="mock",
            )
        user = next(m for m in request.messages if m.role == Role.USER)
        found = re.search(
            r"\[attachment: contract\.docx, ([^,]+), id=([^\]]+)\]", user.text_content
        )
        assert found is not None and found.group(1) == DOCX
        return tool_call_response("docs.extract", {"artifact_id": found.group(2)})

    provider = MockModelProvider(handler=reply)
    runtime = _runtime(await _store(document), provider, enabled_tools=frozenset({"docs.extract"}))
    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="docs.extract",
                version="1",
                description="Extract a document's text",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
            ),
            extract,
        )
    )

    result = await runtime.run(_request(RunAttachment(artifact_id=document.id, media_type=DOCX)))

    assert result.status == RunStatus.COMPLETED, result.error
    assert read == [("contract.docx", document.data)]
    for sent in provider.requests:
        for message in sent.messages:
            assert not any(isinstance(block, FileBlock) for block in message.content)
    # The run keeps the reference, with its real type.
    state = await runtime.runs.get(result.run_id, TENANT)
    assert state.request.attachments == (RunAttachment(artifact_id=document.id, media_type=DOCX),)


class _TextOnly(MockModelProvider):
    provider_id = "text-only"

    async def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(tools=True, images=False, files=False)


async def test_a_text_only_model_takes_files_a_tool_reads() -> None:
    sheet = _artifact(CSV, "days.csv", "text/csv")
    text_only = _TextOnly([])  # echoes the last message
    runtime = _runtime(await _store(sheet), text_only, attachments=_policy("text/csv"))
    runtime.router = ModelRouter()
    runtime.router.register_provider(text_only, registration_id="mock")

    result = await runtime.run(_request(RunAttachment(artifact_id=sheet.id)))

    assert result.status == RunStatus.COMPLETED, result.error
    assert f"[attachment: days.csv, text/csv, id={sheet.id}]" in (result.output or "")
    assert required_capabilities(["text/csv", DOCX]) == frozenset()


@pytest.mark.parametrize(
    ("artifact", "declared", "policy", "error"),
    [
        # The declared type disagrees with the bytes.
        (_artifact(ooxml("xl/workbook.xml"), "a.docx", DOCX), DOCX, None, "but is " + XLSX),
        (_artifact(ooxml("word/document.xml"), "a.csv", "text/csv"), "text/csv", None, "but is"),
        (_artifact(CSV, "a.docx", DOCX), DOCX, None, "declared " + DOCX + " but is text/plain"),
        # Another tenant's file is reported as missing.
        (
            _artifact(ooxml("word/document.xml"), "a.docx", DOCX, tenant_id="b"),
            None,
            None,
            "not found",
        ),
        # A type the agent does not list.
        (_artifact(EML, "a.eml", "message/rfc822"), None, None, "is message/rfc822; allowed"),
        # Text uploaded without a text type it lists.
        (_artifact(CSV, "a.csv", "application/octet-stream"), None, "text/csv", "is text/plain"),
        (_artifact(ole("WordDocument"), "a.doc", "application/msword"), None, None, "msword"),
    ],
)
async def test_a_file_a_tool_reads_is_checked_like_any_attachment(
    artifact: Artifact, declared: str | None, policy: str | None, error: str
) -> None:
    provider = MockModelProvider(["never"])
    agent: dict[str, Any] = {"attachments": _policy(policy)} if policy else {}
    runtime = _runtime(await _store(artifact), provider, **agent)

    result = await runtime.run(
        _request(RunAttachment(artifact_id=artifact.id, media_type=declared))
    )

    assert result.status == RunStatus.FAILED
    assert error in (result.error or "")
    assert provider.requests == []


async def test_the_deployment_can_refuse_types_an_agent_lists() -> None:
    document = _artifact(ooxml("word/document.xml"), "a.docx", DOCX)
    provider = MockModelProvider(["never"])
    runtime = _runtime(await _store(document), provider)
    runtime.attachment_limits = AttachmentLimits(allowed_types=MODEL_ATTACHMENT_TYPES)

    result = await runtime.run(_request(RunAttachment(artifact_id=document.id)))

    assert result.status == RunStatus.FAILED
    assert DOCX in (result.error or "") and provider.requests == []


def test_a_type_is_read_by_the_model_or_by_a_tool_never_both() -> None:
    with pytest.raises(ValidationError, match="listed in both"):
        AttachmentPolicy(types=frozenset({"image/png"}), tool_read_types=frozenset({"image/png"}))
    with pytest.raises(ValidationError, match="a model cannot read text/csv"):
        AttachmentPolicy(types=frozenset({"text/csv"}))
    with pytest.raises(ValidationError, match="cannot be recognised"):
        AttachmentPolicy(tool_read_types=frozenset({"application/zip"}))
    with pytest.raises(ValidationError, match="matches no type"):
        AttachmentPolicy(tool_read_types=["application/vnd.oasis.*"])


def test_a_trailing_star_lists_every_known_type_with_that_prefix() -> None:
    policy = AttachmentPolicy.model_validate(
        {"tool_read_types": ["application/vnd.openxmlformats-officedocument.*", "text/csv"]}
    )

    assert policy.tool_read_types == frozenset({DOCX, XLSX, PPTX, "text/csv"})


def test_an_empty_tool_read_list_leaves_the_serialized_policy_unchanged() -> None:
    assert "tool_read_types" not in AttachmentPolicy(enabled=True).model_dump()
    assert AttachmentPolicy(tool_read_types=frozenset({DOCX})).model_dump()["tool_read_types"]


def test_settings_reject_an_agent_listing_a_type_twice() -> None:
    from algen_agent_runtime.config.settings import AppSettings

    agent = {
        "name": "reader",
        "version": "1.0.0",
        "description": "d",
        "system_instructions": "x",
        "default_model": {"name": "d", "provider": "mock", "model": "m"},
        "attachments": {
            "enabled": True,
            "types": ["application/pdf"],
            "tool_read_types": ["application/pdf"],
        },
    }
    with pytest.raises(ValidationError, match="listed in both"):
        AppSettings.model_validate(
            {"providers": {"mock": {"type": "mock", "default_model": "m"}}, "agents": [agent]}
        )


@pytest.mark.parametrize(
    ("data", "declared", "status", "reason"),
    [
        (ooxml("word/document.xml"), DOCX, ArtifactStatus.AVAILABLE, "clean"),
        (EML, "message/rfc822", ArtifactStatus.AVAILABLE, "clean"),
        (
            ooxml("word/document.xml"),
            "application/pdf",
            ArtifactStatus.QUARANTINED,
            "type_mismatch",
        ),
        (ooxml("word/document.xml"), XLSX, ArtifactStatus.AVAILABLE, "not_inspected"),
    ],
)
async def test_the_basic_scanner_passes_documents_and_quarantines_impostors(
    data: bytes, declared: str, status: ArtifactStatus, reason: str
) -> None:
    result = await BasicArtifactScanner().scan(
        Artifact(tenant_id="t", name="f", media_type=declared, data=data)
    )

    assert (result.status, result.reason_code) == (status, reason)
