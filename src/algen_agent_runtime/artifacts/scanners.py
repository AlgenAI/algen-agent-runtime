"""A built-in scanner for uploads: the real type, the size, and that images are well formed.

It does not inspect content (hidden text, instructions aimed at a model): hosts add that with
their own :class:`ArtifactScanner`, for example over the document inspectors.
"""

from __future__ import annotations

from algen_agent_runtime.artifacts.service import ArtifactScanResult
from algen_agent_runtime.runtime.attachments import file_problem, sniff_media_type
from algen_agent_runtime.types.contracts import Artifact, ArtifactStatus

# Declared types that say nothing about the content, so they never disagree with it.
_GENERIC_TYPES = frozenset({"application/octet-stream", "binary/octet-stream"})


class BasicArtifactScanner:
    """Quarantines an image or PDF whose bytes are not what it claims, are malformed, or exceed
    what a model can be given; makes everything else available.

    A file of a type it does not recognise (text, CSV, Office documents) is made available with
    ``reason_code="not_inspected"``: the agents and tools that read it still apply their own
    checks.
    """

    name = "algen.basic"
    version = "1"

    async def scan(self, artifact: Artifact) -> ArtifactScanResult:
        declared = artifact.media_type.split(";", 1)[0].strip().lower()
        real = sniff_media_type(artifact.data)
        if real is None:
            if declared.startswith("image/") or declared == "application/pdf":
                return self._result(ArtifactStatus.QUARANTINED, "type_unrecognised")
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
