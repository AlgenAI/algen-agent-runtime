from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from algen_agent_runtime.retrieval.contracts import (
    RetrievalMode,
    RetrievalQuery,
    RetrievedDocument,
    SourceDocument,
)
from algen_agent_runtime.retrieval.ingestion import TextChunker
from algen_agent_runtime.retrieval.memory import Embedder

_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_QDRANT_NAMESPACE = UUID("92bb4d3e-e914-4c55-a5d7-09383ef1510d")
_EMBED_BATCH_SIZE = 128
_EMBED_BATCH_CHARACTERS = 200_000


def _check_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"unsafe SQL identifier {value!r}")
    return value


def _storage_id(tenant_id: str, chunk_id: str) -> str:
    return hashlib.sha256(f"{tenant_id}\0{chunk_id}".encode()).hexdigest()


def _payload(document: RetrievedDocument, tenant_id: str) -> dict[str, Any]:
    return {
        "chunk_id": document.id,
        "document_id": document.document_id,
        "text": document.text,
        "source": document.source,
        "title": document.title,
        "uri": document.uri,
        "chunk_index": document.chunk_index,
        "tenant_id": tenant_id,
        "metadata": document.metadata,
    }


def _from_payload(payload: Mapping[str, Any], score: float) -> RetrievedDocument:
    raw_metadata = payload.get("metadata")
    if isinstance(raw_metadata, str):
        try:
            decoded_metadata = json.loads(raw_metadata)
        except json.JSONDecodeError as exc:
            raise ValueError("vector-store metadata contains invalid JSON") from exc
        if not isinstance(decoded_metadata, dict):
            raise ValueError("vector-store metadata JSON must contain an object")
        metadata = decoded_metadata
    elif isinstance(raw_metadata, Mapping):
        metadata = dict(raw_metadata)
    elif raw_metadata is None:
        metadata = {}
    else:
        raise ValueError("vector-store metadata must be an object or JSON object string")
    metadata.setdefault("tenant_id", payload.get("tenant_id"))
    return RetrievedDocument(
        id=str(payload["chunk_id"]),
        document_id=str(payload["document_id"]) if payload.get("document_id") else None,
        text=str(payload.get("text", "")),
        source=str(payload.get("source", "unknown")),
        title=str(payload["title"]) if payload.get("title") else None,
        uri=str(payload["uri"]) if payload.get("uri") else None,
        chunk_index=int(payload.get("chunk_index", 0)),
        score=max(0.0, min(1.0, score)),
        vector_score=max(0.0, min(1.0, score)),
        metadata=metadata,
    )


class _VectorStoreBase:
    def __init__(self, embedder: Embedder, chunker: TextChunker | None = None) -> None:
        self._embedder = embedder
        self._chunker = chunker or TextChunker()

    async def _chunks_and_vectors(
        self, documents: Sequence[SourceDocument]
    ) -> tuple[list[RetrievedDocument], list[list[float]]]:
        chunks = [chunk for document in documents for chunk in self._chunker.chunk(document)]
        if not chunks:
            return [], []
        vectors: list[list[float]] = []
        batch: list[str] = []
        characters = 0
        for chunk in chunks:
            if batch and (
                len(batch) >= _EMBED_BATCH_SIZE
                or characters + len(chunk.text) > _EMBED_BATCH_CHARACTERS
            ):
                vectors.extend(await self._embedder.embed(batch))
                batch = []
                characters = 0
            batch.append(chunk.text)
            characters += len(chunk.text)
        if batch:
            vectors.extend(await self._embedder.embed(batch))
        if len(chunks) != len(vectors):
            raise ValueError("embedder returned a different number of vectors than chunks")
        return chunks, vectors

    @staticmethod
    def _validate_dimensions(vectors: Sequence[Sequence[float]], expected: int) -> None:
        invalid = next((len(vector) for vector in vectors if len(vector) != expected), None)
        if invalid is not None:
            raise ValueError(
                f"embedding dimension mismatch: backend expects {expected}, embedder returned {invalid}"
            )

    @staticmethod
    def _require_vector_mode(query: RetrievalQuery) -> None:
        if query.mode != RetrievalMode.VECTOR:
            raise ValueError("this backend supports vector retrieval mode only")


class PgVectorRetriever(_VectorStoreBase):
    """Tenant-isolated pgvector storage with vector, keyword, and hybrid retrieval."""

    def __init__(
        self,
        dsn: str,
        embedder: Embedder,
        *,
        dimensions: int,
        table: str = "algen_agent_runtime_documents",
        chunker: TextChunker | None = None,
        pool: Any | None = None,
        initialize_schema: bool = True,
    ) -> None:
        super().__init__(embedder, chunker)
        self._dsn = dsn
        self._dimensions = dimensions
        self._table = _check_identifier(table)
        self._pool = pool
        self._owns_pool = pool is None
        self._initialize_schema = initialize_schema
        self._vector_factory: Any = lambda value: value
        self._ready = False
        self._ready_lock = asyncio.Lock()

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        async with self._ready_lock:
            if self._ready:
                return
            if self._pool is None:
                try:
                    import asyncpg
                    from pgvector import Vector
                    from pgvector.asyncpg import register_vector
                except ImportError as exc:
                    raise ImportError("install algen-agent-runtime[pgvector]") from exc
                if self._initialize_schema:
                    bootstrap = await asyncpg.connect(self._dsn)
                    try:
                        await bootstrap.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    finally:
                        await bootstrap.close()
                self._pool = await asyncpg.create_pool(self._dsn, init=register_vector)
                self._vector_factory = Vector
            if self._initialize_schema:
                async with self._pool.acquire() as connection:
                    await connection.execute(
                        f"""CREATE TABLE IF NOT EXISTS {self._table} (
                            tenant_id TEXT NOT NULL,
                            chunk_id TEXT NOT NULL,
                            document_id TEXT NOT NULL,
                            text TEXT NOT NULL,
                            source TEXT NOT NULL,
                            title TEXT,
                            uri TEXT,
                            chunk_index INTEGER NOT NULL,
                            metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                            embedding vector({self._dimensions}) NOT NULL,
                            search_vector tsvector GENERATED ALWAYS AS
                                (to_tsvector('english', text)) STORED,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                            PRIMARY KEY (tenant_id, chunk_id)
                        )"""
                    )
                    await connection.execute(
                        f"CREATE INDEX IF NOT EXISTS {self._table}_document_idx "
                        f"ON {self._table} (tenant_id, document_id)"
                    )
                    await connection.execute(
                        f"CREATE INDEX IF NOT EXISTS {self._table}_search_idx "
                        f"ON {self._table} USING GIN (search_vector)"
                    )
                    await connection.execute(
                        f"CREATE INDEX IF NOT EXISTS {self._table}_embedding_idx "
                        f"ON {self._table} USING hnsw (embedding vector_cosine_ops)"
                    )
            self._ready = True

    async def ingest(self, documents: Sequence[SourceDocument]) -> int:
        await self._ensure_ready()
        assert self._pool is not None
        chunks, vectors = await self._chunks_and_vectors(documents)
        self._validate_dimensions(vectors, self._dimensions)
        owners = {
            (str(chunk.metadata.get("tenant_id", "default")), chunk.document_id) for chunk in chunks
        }
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                for tenant_id, document_id in owners:
                    await connection.execute(
                        f"DELETE FROM {self._table} WHERE tenant_id = $1 AND document_id = $2",
                        tenant_id,
                        document_id,
                    )
                await connection.executemany(
                    f"""INSERT INTO {self._table}
                        (tenant_id, chunk_id, document_id, text, source, title, uri,
                         chunk_index, metadata, embedding)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::vector)""",
                    [
                        (
                            str(chunk.metadata.get("tenant_id", "default")),
                            chunk.id,
                            chunk.document_id or chunk.id,
                            chunk.text,
                            chunk.source,
                            chunk.title,
                            chunk.uri,
                            chunk.chunk_index,
                            json.dumps(chunk.metadata),
                            self._vector_factory(vector),
                        )
                        for chunk, vector in zip(chunks, vectors, strict=True)
                    ],
                )
        return len(chunks)

    async def delete(self, tenant_id: str, document_id: str) -> int:
        await self._ensure_ready()
        assert self._pool is not None
        result = await self._pool.execute(
            f"DELETE FROM {self._table} WHERE tenant_id = $1 AND document_id = $2",
            tenant_id,
            document_id,
        )
        return int(str(result).rsplit(" ", 1)[-1])

    async def retrieve(self, query: RetrievalQuery) -> tuple[RetrievedDocument, ...]:
        await self._ensure_ready()
        assert self._pool is not None
        vectors = await self._embedder.embed([query.text])
        self._validate_dimensions(vectors, self._dimensions)
        vector = self._vector_factory(vectors[0])
        keyword_weight, vector_weight = query.normalized_weights
        clauses = ["tenant_id = $1", "metadata @> $2::jsonb"]
        parameters: list[Any] = [
            query.tenant_id,
            json.dumps(query.filters),
            vector,
            query.text,
            query.limit,
        ]
        score = (
            f"LEAST(1.0, GREATEST(0.0, {vector_weight} * (1 - (embedding <=> $3::vector)) + "
            f"{keyword_weight} * ts_rank_cd(search_vector, plainto_tsquery('english', $4))))"
        )
        rows = await self._pool.fetch(
            f"""SELECT chunk_id, document_id, text, source, title, uri, chunk_index,
                       metadata, tenant_id, {score} AS score
                FROM {self._table}
                WHERE {" AND ".join(clauses)} AND {score} >= {query.minimum_score}
                ORDER BY score DESC, chunk_id LIMIT $5""",
            *parameters,
        )
        return tuple(_from_payload(dict(row), float(row["score"])) for row in rows)

    async def close(self) -> None:
        if self._owns_pool and self._pool is not None and hasattr(self._pool, "close"):
            await self._pool.close()


class ChromaRetriever(_VectorStoreBase):
    def __init__(
        self,
        embedder: Embedder,
        *,
        collection_name: str = "algen_agent_runtime_documents",
        path: str | None = None,
        client: Any | None = None,
        chunker: TextChunker | None = None,
    ) -> None:
        super().__init__(embedder, chunker)
        if client is None:
            try:
                import chromadb
            except ImportError as exc:
                raise ImportError("install algen-agent-runtime[chroma]") from exc
            client = chromadb.PersistentClient(path=path) if path else chromadb.Client()
        self._collection = client.get_or_create_collection(
            collection_name, metadata={"hnsw:space": "cosine"}
        )

    async def ingest(self, documents: Sequence[SourceDocument]) -> int:
        chunks, vectors = await self._chunks_and_vectors(documents)
        for document in documents:
            await self.delete(str(document.metadata.get("tenant_id", "default")), document.id)
        if chunks:
            await asyncio.to_thread(
                self._collection.upsert,
                ids=[
                    _storage_id(str(chunk.metadata.get("tenant_id", "default")), chunk.id)
                    for chunk in chunks
                ],
                embeddings=vectors,
                documents=[chunk.text for chunk in chunks],
                metadatas=[
                    {
                        "tenant_id": str(chunk.metadata.get("tenant_id", "default")),
                        "document_id": chunk.document_id or chunk.id,
                        "payload": json.dumps(
                            _payload(chunk, str(chunk.metadata.get("tenant_id", "default")))
                        ),
                        **{
                            f"filter__{key}": value
                            for key, value in chunk.metadata.items()
                            if isinstance(value, str | int | float | bool)
                        },
                    }
                    for chunk in chunks
                ],
            )
        return len(chunks)

    async def delete(self, tenant_id: str, document_id: str) -> int:
        existing = await asyncio.to_thread(
            self._collection.get,
            where={"$and": [{"tenant_id": tenant_id}, {"document_id": document_id}]},
        )
        ids = list(existing.get("ids", ()))
        if ids:
            await asyncio.to_thread(self._collection.delete, ids=ids)
        return len(ids)

    async def retrieve(self, query: RetrievalQuery) -> tuple[RetrievedDocument, ...]:
        self._require_vector_mode(query)
        vectors = await self._embedder.embed([query.text])
        where_items = [
            {"tenant_id": query.tenant_id},
            *[{f"filter__{key}": value} for key, value in query.filters.items()],
        ]
        where = where_items[0] if len(where_items) == 1 else {"$and": where_items}
        result = await asyncio.to_thread(
            self._collection.query,
            query_embeddings=vectors,
            n_results=query.limit,
            where=where,
            include=["metadatas", "distances"],
        )
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        output = []
        for metadata, distance in zip(metadatas, distances, strict=True):
            score = max(0.0, 1.0 - float(distance))
            if score >= query.minimum_score:
                output.append(_from_payload(json.loads(metadata["payload"]), score))
        return tuple(output)


class FaissRetriever(_VectorStoreBase):
    """Local persistent FAISS index with tenant-aware post-filtering."""

    def __init__(
        self,
        embedder: Embedder,
        *,
        dimensions: int,
        path: str | Path | None = None,
        chunker: TextChunker | None = None,
    ) -> None:
        super().__init__(embedder, chunker)
        try:
            import faiss
            import numpy as np
        except ImportError as exc:
            raise ImportError("install algen-agent-runtime[faiss]") from exc
        self._faiss, self._np = faiss, np
        self._dimensions = dimensions
        self._path = Path(path) if path else None
        self._records: list[dict[str, Any]] = []
        self._index = faiss.IndexFlatIP(dimensions)
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        if not self._path:
            return
        index_path = self._path.with_suffix(".faiss")
        metadata_path = self._path.with_suffix(".json")
        if index_path.exists() and metadata_path.exists():
            self._index = self._faiss.read_index(str(index_path))
            self._records = json.loads(metadata_path.read_text(encoding="utf-8"))

    def _persist(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._faiss.write_index(self._index, str(self._path.with_suffix(".faiss")))
        self._path.with_suffix(".json").write_text(json.dumps(self._records), encoding="utf-8")

    def _rebuild(self) -> None:
        self._index = self._faiss.IndexFlatIP(self._dimensions)
        vectors = [record["_vector"] for record in self._records if "_vector" in record]
        if vectors:
            array = self._np.asarray(vectors, dtype="float32")
            self._faiss.normalize_L2(array)
            self._index.add(array)

    async def ingest(self, documents: Sequence[SourceDocument]) -> int:
        chunks, vectors = await self._chunks_and_vectors(documents)
        self._validate_dimensions(vectors, self._dimensions)
        async with self._lock:
            owners = {
                (str(item.metadata.get("tenant_id", "default")), item.id) for item in documents
            }
            retained = []
            for record in self._records:
                if (record["tenant_id"], record["document_id"]) not in owners:
                    retained.append(record)
            self._records = retained
            for chunk, vector in zip(chunks, vectors, strict=True):
                record = _payload(chunk, str(chunk.metadata.get("tenant_id", "default")))
                record["_vector"] = vector
                self._records.append(record)
            self._rebuild()
            await asyncio.to_thread(self._persist)
        return len(chunks)

    async def delete(self, tenant_id: str, document_id: str) -> int:
        async with self._lock:
            before = len(self._records)
            self._records = [
                record
                for record in self._records
                if not (record["tenant_id"] == tenant_id and record["document_id"] == document_id)
            ]
            removed = before - len(self._records)
            if removed:
                self._rebuild()
                await asyncio.to_thread(self._persist)
            return removed

    async def retrieve(self, query: RetrievalQuery) -> tuple[RetrievedDocument, ...]:
        self._require_vector_mode(query)
        embedded = await self._embedder.embed([query.text])
        self._validate_dimensions(embedded, self._dimensions)
        vector = self._np.asarray(embedded, dtype="float32")
        self._faiss.normalize_L2(vector)
        candidate_count = len(self._records)
        if not candidate_count:
            return ()
        scores, indices = self._index.search(vector, candidate_count)
        results = []
        for score, index in zip(scores[0], indices[0], strict=True):
            if index < 0:
                continue
            record = self._records[int(index)]
            if record["tenant_id"] != query.tenant_id:
                continue
            if any(
                record.get("metadata", {}).get(key) != value for key, value in query.filters.items()
            ):
                continue
            normalized = max(0.0, min(1.0, float(score)))
            if normalized >= query.minimum_score:
                results.append(_from_payload(record, normalized))
            if len(results) >= query.limit:
                break
        return tuple(results)


class QdrantRetriever(_VectorStoreBase):
    def __init__(
        self,
        embedder: Embedder,
        *,
        dimensions: int,
        collection_name: str = "algen_agent_runtime_documents",
        url: str = "http://localhost:6333",
        api_key: str | None = None,
        client: Any | None = None,
        chunker: TextChunker | None = None,
    ) -> None:
        super().__init__(embedder, chunker)
        try:
            from qdrant_client import AsyncQdrantClient, models
        except ImportError as exc:
            raise ImportError("install algen-agent-runtime[qdrant]") from exc
        self._models = models
        self._client = client or AsyncQdrantClient(url=url, api_key=api_key)
        self._owns_client = client is None
        self._collection = collection_name
        self._dimensions = dimensions
        self._ready = False
        self._ready_lock = asyncio.Lock()

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        async with self._ready_lock:
            if not await self._client.collection_exists(self._collection):
                await self._client.create_collection(
                    collection_name=self._collection,
                    vectors_config=self._models.VectorParams(
                        size=self._dimensions, distance=self._models.Distance.COSINE
                    ),
                )
            self._ready = True

    def _filter(self, tenant_id: str, filters: Mapping[str, Any] | None = None) -> Any:
        conditions = [
            self._models.FieldCondition(
                key="tenant_id", match=self._models.MatchValue(value=tenant_id)
            )
        ]
        conditions.extend(
            self._models.FieldCondition(
                key=f"metadata.{key}", match=self._models.MatchValue(value=value)
            )
            for key, value in (filters or {}).items()
        )
        return self._models.Filter(must=conditions)

    async def ingest(self, documents: Sequence[SourceDocument]) -> int:
        await self._ensure_ready()
        chunks, vectors = await self._chunks_and_vectors(documents)
        self._validate_dimensions(vectors, self._dimensions)
        for document in documents:
            await self.delete(str(document.metadata.get("tenant_id", "default")), document.id)
        if chunks:
            await self._client.upsert(
                collection_name=self._collection,
                points=[
                    self._models.PointStruct(
                        id=str(
                            uuid5(
                                _QDRANT_NAMESPACE,
                                f"{chunk.metadata.get('tenant_id', 'default')}:{chunk.id}",
                            )
                        ),
                        vector=vector,
                        payload=_payload(chunk, str(chunk.metadata.get("tenant_id", "default"))),
                    )
                    for chunk, vector in zip(chunks, vectors, strict=True)
                ],
                wait=True,
            )
        return len(chunks)

    async def delete(self, tenant_id: str, document_id: str) -> int:
        await self._ensure_ready()
        points, _ = await self._client.scroll(
            collection_name=self._collection,
            scroll_filter=self._models.Filter(
                must=[
                    self._models.FieldCondition(
                        key="tenant_id", match=self._models.MatchValue(value=tenant_id)
                    ),
                    self._models.FieldCondition(
                        key="document_id", match=self._models.MatchValue(value=document_id)
                    ),
                ]
            ),
            limit=10_000,
            with_payload=False,
        )
        if points:
            await self._client.delete(
                collection_name=self._collection,
                points_selector=self._models.PointIdsList(points=[point.id for point in points]),
                wait=True,
            )
        return len(points)

    async def retrieve(self, query: RetrievalQuery) -> tuple[RetrievedDocument, ...]:
        self._require_vector_mode(query)
        await self._ensure_ready()
        vectors = await self._embedder.embed([query.text])
        self._validate_dimensions(vectors, self._dimensions)
        response = await self._client.query_points(
            collection_name=self._collection,
            query=vectors[0],
            query_filter=self._filter(query.tenant_id, query.filters),
            limit=query.limit,
            score_threshold=query.minimum_score,
            with_payload=True,
        )
        return tuple(
            _from_payload(point.payload or {}, float(point.score)) for point in response.points
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()
