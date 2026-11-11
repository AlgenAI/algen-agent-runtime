"""A built-in scanner for uploads: the real type, the size, and that images are well formed.

It does not inspect content (hidden text, instructions aimed at a model): hosts add that with
their own :class:`ArtifactScanner`, for example over the document inspectors.
"""

from __future__ import annotations

from algen_agent_runtime.artifacts.service import ArtifactScanResult
from algen_agent_runtime.runtime.attachments import (
    accepted_media_type,
    file_problem,
    sniff_media_type,
)
from algen_agent_runtime.types.contracts import MODEL_ATTACHMENT_TYPES, Artifact, ArtifactStatus

# Declared types that say nothing about the content, so they never disagree with it.
_GENERIC_TYPES = frozenset({"application/octet-stream", "binary/octet-stream"})


class BasicArtifactScanner:
    """Quarantines an image or PDF whose bytes are not what it claims, are malformed, or exceed
    what a model can be given; makes everything else available.

    Office documents, e-mail and text are made available (``clean`` when the declared type agrees
    with the bytes, ``not_inspected`` otherwise), unless they pose as an image or PDF; a file of a
    type it does not recognise is made available with ``reason_code="not_inspected"``. The agents
    and tools that read them still apply their own checks.
    """

    name = "algen.basic"
    version = "1"

    async def scan(self, artifact: Artifact) -> ArtifactScanResult:
        declared = artifact.media_type.split(";", 1)[0].strip().lower()
        real = sniff_media_type(artifact.data)
        claims_model_type = declared.startswith("image/") or declared == "application/pdf"
        if real is None:
            if claims_model_type:
                return self._result(ArtifactStatus.QUARANTINED, "type_unrecognised")
            return self._result(ArtifactStatus.AVAILABLE, "not_inspected")
        if real not in MODEL_ATTACHMENT_TYPES and real != "image/heic":
            # Office documents, e-mail and text: never sent to a model, so only a file posing as
            # an image or PDF is quarantined. A run still refuses a declared type that disagrees.
            if claims_model_type:
                return self._result(ArtifactStatus.QUARANTINED, "type_mismatch")
            if declared in _GENERIC_TYPES or accepted_media_type(real, declared) is not None:
                return self._result(ArtifactStatus.AVAILABLE, "clean")
            return self._result(ArtifactStatus.AVAILABLE, "not_inspected")
        if declared != real and declared not in _GENERIC_TYPES:
            return self._result(ArtifactStatus.QUARANTINED, "type_mismatch")
        if file_problem(artifact.data, real) is not None:
            return self._result(ArtifactStatus.QUARANTINED, "malformed_or_too_large")
        return self._result(ArtifactStatus.AVAILABLE, "clean")

    def _result(self, status: ArtifactStatus, reason: str) -> ArtifactScanResult:
        return ArtifactScanResult(
            status=status, scanner=self.name, scanner_version=self.version, reason_code=reason
        )
