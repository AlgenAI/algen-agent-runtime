from __future__ import annotations

import asyncio

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.retrieval.contracts import (
    RetrievalMode,
    RetrievalQuery,
    SourceDocument,
)
from algen_agent_runtime.retrieval.ingestion import TextChunker
from algen_agent_runtime.retrieval.memory import InMemoryRetriever


def source(
    document_id: str,
    text: str,
    *,
    tenant_id: str = "tenant-a",
    category: str = "handbook",
) -> SourceDocument:
    return SourceDocument(
        id=document_id,
        text=text,
        source=f"kb://{document_id}",
        title=document_id.replace("-", " ").title(),
        metadata={"tenant_id": tenant_id, "category": category},
    )


def test_text_chunker_preserves_source_metadata_and_overlap() -> None:
    document = source("leave", " ".join(f"word{index}" for index in range(80)))
    chunks = TextChunker(chunk_size=100, chunk_overlap=20).chunk(document)

    assert len(chunks) > 1
    assert chunks[0].document_id == "leave"
    assert chunks[0].metadata["tenant_id"] == "tenant-a"
    assert chunks[0].id == "leave:0"
    assert set(chunks[0].text.split()) & set(chunks[1].text.split())


async def test_hybrid_retrieval_filters_tenant_metadata_and_deduplicates() -> None:
    retriever = InMemoryRetriever()
    await retriever.ingest(
        (
            source("refund", "Refund requests are accepted within thirty days."),
            source("security", "Security incidents must be reported immediately."),
            source(
                "other-tenant",
                "Refund requests have a ninety day window.",
                tenant_id="tenant-b",
            ),
            source(
                "engineering",
                "Refund service deployment guide.",
                category="engineering",
            ),
        )
    )

    results = await retriever.retrieve(
        RetrievalQuery(
            text="What is the refund request window?",
            tenant_id="tenant-a",
            mode=RetrievalMode.HYBRID,
            filters={"category": "handbook"},
        )
    )

    assert results
    assert results[0].document_id == "refund"
    assert all(item.metadata["tenant_id"] == "tenant-a" for item in results)
    assert all(item.metadata["category"] == "handbook" for item in results)
    assert results[0].keyword_score > 0
    assert results[0].vector_score > 0


async def test_tenant_delete_cannot_remove_another_tenants_document() -> None:
    retriever = InMemoryRetriever()
    await retriever.ingest((source("private", "Tenant-specific policy."),))

    assert await retriever.delete("tenant-b", "private") == 0
    assert await retriever.delete("tenant-a", "private") == 1
    assert not await retriever.retrieve(RetrievalQuery(text="policy", tenant_id="tenant-a"))


async def test_concurrent_lazy_retrieval_waits_for_single_ingestion() -> None:
    retriever = InMemoryRetriever(
        source_documents=(source("leave", "Annual leave is twenty days."),)
    )
    query = RetrievalQuery(text="annual leave", tenant_id="tenant-a")

    first, second = await asyncio.gather(retriever.retrieve(query), retriever.retrieve(query))

    assert first[0].document_id == "leave"
    assert second[0].document_id == "leave"


async def test_reingestion_removes_stale_chunks() -> None:
    retriever = InMemoryRetriever(chunker=TextChunker(chunk_size=64, chunk_overlap=8))
    await retriever.ingest((source("policy", "leave " * 100),))
    await retriever.ingest((source("policy", "A short replacement policy."),))

    results = await retriever.retrieve(
        RetrievalQuery(text="leave replacement policy", tenant_id="tenant-a", limit=100)
    )

    assert len(results) == 1
    assert results[0].text == "A short replacement policy."


def test_retrieval_configuration_builds_named_context_and_retriever() -> None:
    settings = AppSettings.model_validate(
        {
            "retrieval": {
                "knowledge": {
                    "mode": "hybrid",
                    "chunk_size": 256,
                    "chunk_overlap": 32,
                    "documents": [
                        {
                            "id": "policy",
                            "text": "The support desk operates continuously.",
                            "source": "kb://policy",
                            "metadata": {"tenant_id": "tenant-a"},
                        }
                    ],
                }
            }
        }
    )

    container = build_container(settings)
    assert container.retrievers.list() == ("knowledge",)
    assert container.runtime.contexts.get("retrieval.knowledge").name == "retrieval.knowledge"
