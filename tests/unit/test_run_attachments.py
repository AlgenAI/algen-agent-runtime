"""Files given to a run (#82): checked before any model call, held by reference, loaded at
dispatch, and retired from memory after the turn."""

from __future__ import annotations

import struct
import zlib
from typing import Any

import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.policies.contracts import PolicyAction, PolicyDecision, PolicyPoint
from algen_agent_runtime.runtime.attachments import (
    DATA_NOTE,
    resolve,
    sniff_media_type,
    user_message,
)
from algen_agent_runtime.types.contracts import (
    Artifact,
    ArtifactStatus,
    AttachmentPolicy,
    FileBlock,
    MemoryPolicy,
    ModelCapabilities,
    ModelProfile,
    ModelRequest,
    RunAttachment,
    RunRequest,
    RunStatus,
    TextBlock,
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


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"


async def _store(*artifacts: Artifact) -> InMemoryArtifactStore:
    store = InMemoryArtifactStore()
    for artifact in artifacts:
        await store.put(artifact)
    return store


def _artifact(data: bytes, name: str = "passport.png", **values: Any) -> Artifact:
    return Artifact(
        tenant_id=values.pop("tenant_id", TENANT),
        name=name,
        media_type=values.pop("media_type", "image/png"),
        data=data,
        **values,
    )


def _runtime(
    store: InMemoryArtifactStore,
    provider: MockModelProvider | None = None,
    **agent: Any,
) -> tuple[Any, MockModelProvider]:
    provider = provider or MockModelProvider(["It is a passport."])
    agent.setdefault("attachments", AttachmentPolicy(enabled=True))
    runtime = make_runtime(provider, make_agent(**agent))
    runtime.artifacts = store
    return runtime, provider


def _request(*attachments: RunAttachment, **values: Any) -> RunRequest:
    return RunRequest(
        agent="test-agent",
        input="What document is this?",
        tenant_id=values.pop("tenant_id", TENANT),
        user_id="u",
        attachments=attachments,
        **values,
    )


async def test_the_model_reads_an_attached_image_and_the_run_stores_only_its_reference() -> None:
    image = _artifact(png())
    runtime, provider = _runtime(await _store(image))

    result = await runtime.run(_request(RunAttachment(artifact_id=image.id)))

    assert result.status == RunStatus.COMPLETED
    [sent] = provider.requests
    user = next(message for message in sent.messages if message.role.value == "user")
    [file] = [block for block in user.content if isinstance(block, FileBlock)]
    assert file.content == image.data and file.media_type == "image/png"
    assert any(isinstance(b, TextBlock) and b.text == DATA_NOTE for b in user.content)
    state = await runtime.runs.get(result.run_id, TENANT)
    stored = state.model_dump_json()
    assert image.id in stored
    # Neither the bytes nor their base64 encoding reach the checkpoint.
    assert "iVBORw0KGgo" not in stored and "\\x89PNG" not in stored


async def test_a_text_only_run_is_unchanged() -> None:
    runtime, provider = _runtime(await _store(), attachments=AttachmentPolicy())

    await runtime.run(_request())

    user = next(m for m in provider.requests[0].messages if m.role.value == "user")
    assert user.content == (TextBlock(text="What document is this?"),)
    assert "attachments" not in _request().model_dump()


@pytest.mark.parametrize(
    ("artifact", "attachment", "error"),
    [
        (_artifact(png(), status=ArtifactStatus.PENDING_SCAN), None, "pending_scan"),
        (_artifact(png(), tenant_id="tenant-b"), None, "was not found"),
        (_artifact(HEIC, name="photo.heic", media_type="image/heic"), None, "image/heic"),
        (_artifact(png()), "image/jpeg", "declared image/jpeg but is image/png"),
        (_artifact(png(9000, 10)), None, "at most 8000 per side"),
        (_artifact(b"not an image", media_type="image/png"), None, "unrecognised type"),
    ],
)
async def test_a_file_the_run_cannot_take_is_refused_before_any_model_call(
    artifact: Artifact, attachment: str | None, error: str
) -> None:
    runtime, provider = _runtime(await _store(artifact))

    result = await runtime.run(
        _request(RunAttachment(artifact_id=artifact.id, media_type=attachment))
    )

    assert result.status == RunStatus.FAILED
    assert error in (result.error or "")
    assert provider.requests == []


async def test_an_agent_that_did_not_opt_in_refuses_files() -> None:
    image = _artifact(png())
    runtime, provider = _runtime(await _store(image), attachments=AttachmentPolicy())

    result = await runtime.run(_request(RunAttachment(artifact_id=image.id)))

    assert result.status == RunStatus.FAILED
    assert "does not accept attachments" in (result.error or "")
    assert provider.requests == []


async def test_an_agent_limits_the_types_and_count_it_takes() -> None:
    document = _artifact(PDF, name="invoice.pdf", media_type="application/pdf")
    images_only = AttachmentPolicy(enabled=True, types=frozenset({"image/png"}), max_count=1)
    runtime, _ = _runtime(await _store(document), attachments=images_only)

    result = await runtime.run(_request(RunAttachment(artifact_id=document.id)))

    assert result.status == RunStatus.FAILED
    assert "application/pdf" in (result.error or "")


class _TextOnly(MockModelProvider):
    provider_id = "text-only"

    async def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(tools=True, images=False, files=False)


async def test_a_model_that_cannot_read_images_is_never_given_one() -> None:
    image = _artifact(png())
    text_only = _TextOnly(["never"])
    runtime, _ = _runtime(await _store(image), provider=text_only)
    runtime.router = ModelRouter()
    runtime.router.register_provider(text_only, registration_id="mock")

    result = await runtime.run(_request(RunAttachment(artifact_id=image.id)))

    assert result.status == RunStatus.FAILED
    assert "can read the attachments" in (result.error or "")
    assert text_only.requests == []


async def test_a_fallback_model_without_vision_is_skipped() -> None:
    image = _artifact(png())
    vision = MockModelProvider(["Passport."])
    text_only = _TextOnly(["never"])
    router = ModelRouter()
    router.register_provider(text_only, registration_id="text")
    router.register_provider(vision, registration_id="vision")
    runtime, _ = _runtime(
        await _store(image),
        default_model=ModelProfile(name="t", provider="text", model="m"),
        fallback_models=(ModelProfile(name="v", provider="vision", model="m"),),
    )
    runtime.router = router

    result = await runtime.run(_request(RunAttachment(artifact_id=image.id)))

    assert result.status == RunStatus.COMPLETED
    assert text_only.requests == [] and len(vision.requests) == 1


async def test_a_file_deleted_after_validation_becomes_a_note() -> None:
    image = _artifact(png())
    store = await _store(image)
    request = ModelRequest(messages=(user_message(_request(RunAttachment(artifact_id=image.id))),))
    await store.delete(image.id, TENANT)

    resolved = await resolve(request, TENANT, store)

    texts = [b.text for b in resolved.messages[0].content if isinstance(b, TextBlock)]
    assert f"[attachment {image.id} is no longer available]" in texts
    # Another tenant's request never loads the file either.
    await store.put(image)
    other = await resolve(request, "tenant-b", store)
    assert not any(isinstance(b, FileBlock) and b.content for b in other.messages[0].content)


async def test_turn_retention_leaves_a_note_in_memory_and_session_keeps_the_file() -> None:
    image = _artifact(png())
    for retention, expected in (("turn", TextBlock), ("session", FileBlock)):
        runtime, _ = _runtime(
            await _store(image),
            attachments=AttachmentPolicy(enabled=True, retention=retention),
            memory_policy=MemoryPolicy(enabled=True),
        )
        result = await runtime.run(
            _request(RunAttachment(artifact_id=image.id), session_id=f"s-{retention}")
        )
        assert result.status == RunStatus.COMPLETED
        remembered = await runtime.memory.get(TENANT, f"s-{retention}", 10)
        user = next(message for message in remembered if message.role.value == "user")
        assert isinstance(user.content[-1], expected), retention
        if expected is TextBlock:
            assert user.content[-1].text == "[attached passport.png, already reviewed]"


class _NoPdfs:
    name = "no_pdfs"
    category = "attachments"
    enforcement_mode = "block"
    applies_to = frozenset({PolicyPoint.ATTACHMENTS})

    async def evaluate(self, point: str, payload: Any, context: Any) -> PolicyDecision:
        kept = [item for item in payload if item["media_type"] != "application/pdf"]
        if len(kept) == len(payload):
            return PolicyDecision(action=PolicyAction.ALLOW, reason_code="ok", reason="ok")
        return PolicyDecision(
            action=PolicyAction.TRANSFORM, reason_code="no_pdfs", reason="No PDFs.", value=kept
        )


async def test_an_attachments_policy_can_drop_files() -> None:
    image = _artifact(png())
    document = _artifact(PDF, name="invoice.pdf", media_type="application/pdf")
    runtime, provider = _runtime(
        await _store(image, document),
        guardrail_policy={"policies": ["no_pdfs"]},
    )
    runtime.policies.register(_NoPdfs())

    result = await runtime.run(
        _request(RunAttachment(artifact_id=image.id), RunAttachment(artifact_id=document.id))
    )

    assert result.status == RunStatus.COMPLETED
    user = next(m for m in provider.requests[0].messages if m.role.value == "user")
    assert [b.artifact_id for b in user.content if isinstance(b, FileBlock)] == [image.id]


def test_the_real_type_is_read_from_the_bytes() -> None:
    assert sniff_media_type(png()) == "image/png"
    assert sniff_media_type(PDF) == "application/pdf"
    assert sniff_media_type(HEIC) == "image/heic"
    assert sniff_media_type(b"<html>") is None


async def test_the_mock_shows_files_it_cannot_read_as_text() -> None:
    image = _artifact(png())
    provider = MockModelProvider([])  # echoes the last message
    runtime, _ = _runtime(await _store(image), provider=provider)

    result = await runtime.run(_request(RunAttachment(artifact_id=image.id)))

    assert "[attachment: passport.png, image/png]" in (result.output or "")
