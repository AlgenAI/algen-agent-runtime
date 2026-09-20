from __future__ import annotations

from collections.abc import Sequence

import pytest
from pydantic import ValidationError

from algen_agent_runtime.config.settings import AppSettings, RetrievalSettings
from algen_agent_runtime.retrieval.contracts import RetrievalMode, RetrievalQuery, SourceDocument
from algen_agent_runtime.retrieval.memory import HashingEmbedder
from algen_agent_runtime.retrieval.vector_stores import ChromaRetriever, PgVectorRetriever
from examples.pattern_text_to_sql.advanced.knowledge import TENANT_ID, schema_documents


def test_pgvector_configuration_requires_connection_url() -> None:
    with pytest.raises(ValidationError, match="connection_url"):
        RetrievalSettings(type="pgvector")


def test_all_vector_store_types_are_validated() -> None:
    assert RetrievalSettings(type="memory").type == "memory"
    for backend in ("chroma", "faiss", "qdrant"):
        assert RetrievalSettings(type=backend, mode="vector").type == backend
    assert (
        RetrievalSettings(type="pgvector", connection_url="env://PGVECTOR_DSN").type == "pgvector"
    )


def test_pgvector_text_to_sql_knowledge_is_tenant_scoped_and_semantic() -> None:
    documents = schema_documents()
    assert len(documents) >= 4
    assert all(document.metadata["tenant_id"] == TENANT_ID for document in documents)
    assert any("completed revenue" in document.text.lower() for document in documents)
    assert any(document.metadata.get("object_type") == "table" for document in documents)


def test_persistent_backend_rejects_inline_documents_at_composition() -> None:
    settings = AppSettings.model_validate(
        {
            "retrieval": {
                "knowledge": {
                    "type": "pgvector",
                    "connection_url": "postgresql://localhost/example",
                    "documents": [
                        {
                            "id": "doc",
                            "text": "content",
                            "source": "test",
                            "metadata": {"tenant_id": "tenant-a"},
                        }
                    ],
                }
            }
        }
    )
    from algen_agent_runtime.orchestration.container import build_container

    with pytest.raises(ValueError, match="populated explicitly"):
        build_container(settings)


class FakeChromaCollection:
    def __init__(self) -> None:
        self.records: dict[str, tuple[list[float], dict[str, object]]] = {}

    def upsert(self, **kwargs: object) -> None:
        ids = kwargs["ids"]
        embeddings = kwargs["embeddings"]
        metadatas = kwargs["metadatas"]
        for identifier, vector, metadata in zip(ids, embeddings, metadatas, strict=True):  # type: ignore[arg-type]
            self.records[str(identifier)] = (list(vector), dict(metadata))

    def get(self, *, where: dict[str, object]) -> dict[str, list[str]]:
        conditions = where["$and"]  # type: ignore[index]
        ids = [
            identifier
            for identifier, (_, metadata) in self.records.items()
            if all(
                metadata.get(key) == value
                for condition in conditions
                for key, value in condition.items()
            )  # type: ignore[union-attr]
        ]
        return {"ids": ids}

    def delete(self, *, ids: list[str]) -> None:
        for identifier in ids:
            self.records.pop(identifier, None)

    def query(self, **kwargs: object) -> dict[str, list[list[object]]]:
        where = kwargs["where"]
        conditions = where.get("$and", [where])  # type: ignore[union-attr]
        selected = [
            metadata
            for _, metadata in self.records.values()
            if all(
                metadata.get(key) == value
                for condition in conditions
                for key, value in condition.items()
            )
        ]
        return {"metadatas": [selected], "distances": [[0.1] * len(selected)]}


class FakeChromaClient:
    def __init__(self) -> None:
        self.collection = FakeChromaCollection()

    def get_or_create_collection(self, name: str, metadata: object) -> FakeChromaCollection:
        del name, metadata
        return self.collection


async def test_chroma_adapter_namespaces_ids_and_filters_tenants() -> None:
    client = FakeChromaClient()
    retriever = ChromaRetriever(HashingEmbedder(), client=client, chunker=None)
    common = {"id": "same-id", "text": "refund policy", "source": "test"}
    await retriever.ingest(
        (
            SourceDocument(**common, metadata={"tenant_id": "tenant-a", "category": "policy"}),
            SourceDocument(**common, metadata={"tenant_id": "tenant-b", "category": "policy"}),
        )
    )

    results = await retriever.retrieve(
        RetrievalQuery(
            text="refund",
            tenant_id="tenant-a",
            mode=RetrievalMode.VECTOR,
            filters={"category": "policy"},
        )
    )

    assert len(client.collection.records) == 2
    assert len(results) == 1
    assert results[0].metadata["tenant_id"] == "tenant-a"


class FakePgPool:
    def __init__(self) -> None:
        self.parameters: tuple[object, ...] = ()
        self.statement = ""

    async def fetch(self, statement: str, *parameters: object) -> list[dict[str, object]]:
        self.statement = statement
        self.parameters = parameters
        return [
            {
                "chunk_id": "schema:0",
                "document_id": "schema",
                "text": "orders status completed",
                "source": "catalog://orders",
                "title": "orders",
                "uri": None,
                "chunk_index": 0,
                "metadata": '{"tenant_id":"tenant-a","object_type":"table"}',
                "tenant_id": "tenant-a",
                "score": 0.8,
            }
        ]


async def test_pgvector_adapter_parameterizes_tenant_and_filters() -> None:
    pool = FakePgPool()
    retriever = PgVectorRetriever(
        "unused",
        HashingEmbedder(128),
        dimensions=128,
        pool=pool,
        initialize_schema=False,
    )

    results = await retriever.retrieve(
        RetrievalQuery(
            text="completed orders",
            tenant_id="tenant-a",
            filters={"object_type": "table"},
        )
    )

    assert pool.parameters[0] == "tenant-a"
    assert '"object_type": "table"' in str(pool.parameters[1])
    assert "tenant_id = $1" in pool.statement
    assert results[0].document_id == "schema"
    assert results[0].metadata == {
        "tenant_id": "tenant-a",
        "object_type": "table",
    }


def test_pgvector_rejects_unsafe_table_identifier() -> None:
    with pytest.raises(ValueError, match="unsafe SQL identifier"):
        PgVectorRetriever("unused", HashingEmbedder(), dimensions=128, table="docs; DROP TABLE x")


async def test_vector_store_batches_large_embedding_ingestion() -> None:
    class RecordingEmbedder:
        def __init__(self) -> None:
            self.batch_sizes: list[int] = []

        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            values = list(texts)
            self.batch_sizes.append(len(values))
            return [[0.0] * 8 for _ in values]

    embedder = RecordingEmbedder()
    retriever = ChromaRetriever(embedder, client=FakeChromaClient(), chunker=None)
    documents = tuple(
        SourceDocument(id=f"doc-{index}", text="evidence", source="test") for index in range(260)
    )

    chunks, vectors = await retriever._chunks_and_vectors(documents)

    assert len(chunks) == len(vectors) == 260
    assert embedder.batch_sizes == [128, 128, 4]
