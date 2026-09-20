from __future__ import annotations

from types import SimpleNamespace

from algen_agent_runtime.types.contracts import Citation
from algen_agent_runtime.verification.verifiers import CitationVerifier


def state() -> SimpleNamespace:
    return SimpleNamespace(
        citations=[
            Citation(
                id="S1",
                document_id="doc",
                chunk_id="doc:0",
                source="kb://doc",
                score=0.9,
            )
        ]
    )


async def test_citation_verifier_accepts_known_source() -> None:
    result = await CitationVerifier().verify("Grounded answer [S1].", {"state": state()})
    assert result.passed is True
    assert result.reason_code == "citations.valid"


async def test_citation_verifier_rejects_missing_and_unknown_sources() -> None:
    missing = await CitationVerifier().verify("Unsupported answer.", {"state": state()})
    unknown = await CitationVerifier().verify("Unsupported answer [S9].", {"state": state()})
    assert missing.reason_code == "citations.missing"
    assert unknown.reason_code == "citations.unknown"
