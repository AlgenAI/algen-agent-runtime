"""Attach files to a run, offline: an image the model reads and a CSV a tool reads.

The script makes both files itself, stages them as artifacts and scans them with the basic upload
scanner, then runs an agent with them attached by reference. The mock provider's replies are
scripted with ``MockModelProvider.queue`` (``examples/offline.py``): it asks the reader tool for
the CSV by the id in its placeholder, then answers. The script prints what the model received for
each file, what the run stored, and what the tool read.
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from algen_agent_runtime.artifacts import BasicArtifactScanner
from algen_agent_runtime.artifacts.service import ArtifactLifecycleService
from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import Container, build_container
from algen_agent_runtime.types.contracts import (
    Artifact,
    ArtifactDescriptor,
    ArtifactStatus,
    FileBlock,
    Role,
    RunAttachment,
    RunRequest,
    TextBlock,
)
from examples.offline import script

CONFIG_PATH = Path(__file__).with_name("agent.yaml")
TENANT, USER = "quickstart", "clerk"
ANSWER = "A 4x4 test image and a leave sheet with 2 rows: Ana takes 3 days, Ben 1."


def make_png(width: int = 4, height: int = 4) -> bytes:
    """A small solid-colour PNG, built here so the repository holds no binary file."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    row = b"\x00" + b"\x2a\x6f\xd8" * width
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


CSV = b"name,days\nAna,3\nBen,1\n"


async def stage(
    container: Container, name: str, media_type: str, data: bytes
) -> ArtifactDescriptor:
    """Store a file as an upload would (pending_scan) and scan it, as POST /v1/artifacts does."""
    artifact = Artifact(
        tenant_id=TENANT,
        name=name,
        media_type=media_type,
        data=data,
        status=ArtifactStatus.PENDING_SCAN,
    )
    await container.artifacts.put(artifact)
    return await ArtifactLifecycleService(container.artifacts, container.runtime.audits).scan(
        artifact.id, TENANT, USER, BasicArtifactScanner()
    )


async def demonstrate(question: str) -> dict[str, Any]:
    container = build_container(load_settings((CONFIG_PATH,)))
    try:
        image = await stage(container, "test-card.png", "image/png", make_png())
        sheet = await stage(container, "leave.csv", "text/csv", CSV)
        staged = [
            {"name": d.name, "sha256": d.sha256, "size_bytes": d.size_bytes, "scan": d.status.value}
            for d in (image, sheet)
        ]
        # The model asks the reader tool for the CSV by its id, then answers.
        script(
            container,
            "mock",
            tool_call_response("files.read_text", {"artifact_id": sheet.id}),
            ANSWER,
        )
        result = await container.runtime.run(
            RunRequest(
                agent="document-reader",
                input=question,
                tenant_id=TENANT,
                user_id=USER,
                attachments=(
                    RunAttachment(artifact_id=image.id, media_type="image/png"),
                    RunAttachment(artifact_id=sheet.id),
                ),
            )
        )
        if result.error:
            raise RuntimeError(result.error)

        model = container.runtime.router.provider("mock")
        assert isinstance(model, MockModelProvider)
        first_call = next(m for m in model.requests[0].messages if m.role == Role.USER)
        received = [
            f"file {block.media_type}: {len(block.content or b'')} bytes, loaded for this call only"
            if isinstance(block, FileBlock)
            else block.text
            for block in first_call.content
            if isinstance(block, FileBlock | TextBlock)
        ]
        state = await container.runtime.runs.get(result.run_id, TENANT)
        assert state is not None
        stored_user = next(m for m in state.messages if m.role == Role.USER)
        stored = [
            block.model_dump(mode="json")
            for block in stored_user.content
            if isinstance(block, FileBlock)
        ]
        tool_result = next(m for m in state.messages if m.role == Role.TOOL).text_content
        return {
            "staged": staged,
            "model_received": received,
            "run_stored": stored,
            "tool_read": tool_result,
            "answer": result.output or "",
        }
    finally:
        await container.aclose()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the attachments quickstart (offline)")
    parser.add_argument("question", nargs="?", default="What are these files?")
    arguments = parser.parse_args(argv)
    print("[offline: scripted mock model; scanning, checks and storage are real]")
    shown = asyncio.run(demonstrate(arguments.question))
    print("\nStaged and scanned:")
    for item in shown["staged"]:
        print(
            f"  {item['name']}: {item['size_bytes']} bytes, sha256 {item['sha256'][:16]}..., "
            f"scan {item['scan']}"
        )
    print("\nThe model received (first call):")
    for line in shown["model_received"]:
        print(f"  {line}")
    print("\nThe run stored (references, never bytes):")
    for block in shown["run_stored"]:
        print(f"  {block}")
    print(f"\nThe reader tool returned: {shown['tool_read']}")
    print(f"\nAnswer: {shown['answer']}")


if __name__ == "__main__":
    main()
