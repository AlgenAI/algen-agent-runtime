from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

from algen_agent_runtime.retrieval.contracts import (
    RetrievalMode,
    RetrievalQuery,
    RetrievedDocument,
    SourceDocument,
)
from algen_agent_runtime.retrieval.ingestion import TextChunker

_TOKEN = re.compile(r"\w+")


class Embedder(Protocol):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class ModelEmbeddingProvider(Protocol):
    async def embed(self, texts: Sequence[str], model: str | None = None) -> list[list[float]]: ...


class Reranker(Protocol):
    async def rerank(
        self, query: str, documents: Sequence[RetrievedDocument]
    ) -> Sequence[RetrievedDocument]: ...


class HashingEmbedder:
    """Local deterministic embedder intended for tests and lightweight deployments."""

    def __init__(self, dimensions: int = 128) -> None:
        if dimensions < 8:
            raise ValueError("dimensions must be at least 8")
        self._dimensions = dimensions

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        for token in _TOKEN.findall(text.lower()):
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimensions
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector


class ModelProviderEmbedder:
    def __init__(self, provider: ModelEmbeddingProvider, model: str | None = None) -> None:
        self._provider = provider
        self._model = model

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._provider.embed(texts, self._model)


class InMemoryRetriever:
    """Tenant-aware keyword, vector, and hybrid retriever with lazy ingestion."""

    def __init__(
        self,
        documents: tuple[RetrievedDocument, ...] = (),
        *,
        source_documents: tuple[SourceDocument, ...] = (),
        embedder: Embedder | None = None,
        chunker: TextChunker | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self._documents = list(documents)
        self._vectors: dict[str, list[float]] = {}
        self._pending = list(source_documents)
        self._embedder = embedder or HashingEmbedder()
        self._chunker = chunker or TextChunker()
        self._reranker = reranker
        self._lock = asyncio.Lock()
        self._ingest_lock = asyncio.Lock()

    def add(self, document: RetrievedDocument) -> None:
        """Backward-compatible keyword-only insertion; prefer ``ingest``."""
        self._documents.append(document)

    def add_source(self, document: SourceDocument) -> None:
        self._pending.append(document)

    async def ingest(self, documents: Sequence[SourceDocument]) -> int:
        chunks = [chunk for document in documents for chunk in self._chunker.chunk(document)]
        if not chunks:
            return 0
        vectors = await self._embedder.embed([chunk.text for chunk in chunks])
        if len(vectors) != len(chunks):
            raise ValueError("embedder returned a different number of vectors than input texts")
        async with self._lock:
            owners = {(chunk.document_id, chunk.metadata.get("tenant_id")) for chunk in chunks}
            stale_ids = {
                item.id
                for item in self._documents
                if (item.document_id, item.metadata.get("tenant_id")) in owners
            }
            self._documents = [item for item in self._documents if item.id not in stale_ids]
            for chunk_id in stale_ids:
                self._vectors.pop(chunk_id, None)
            self._documents.extend(chunks)
            self._vectors.update(zip((chunk.id for chunk in chunks), vectors, strict=True))
        return len(chunks)

    async def delete(self, tenant_id: str, document_id: str) -> int:
        async with self._lock:
            pending_count = sum(
                1
                for item in self._pending
                if item.id == document_id and item.metadata.get("tenant_id") == tenant_id
            )
            self._pending = [
                item
                for item in self._pending
                if not (item.id == document_id and item.metadata.get("tenant_id") == tenant_id)
            ]
            selected = [
                item
                for item in self._documents
                if item.document_id == document_id and item.metadata.get("tenant_id") == tenant_id
            ]
            selected_ids = {item.id for item in selected}
            self._documents = [item for item in self._documents if item.id not in selected_ids]
            for chunk_id in selected_ids:
                self._vectors.pop(chunk_id, None)
            return pending_count + len(selected_ids)

    async def retrieve(self, query: RetrievalQuery) -> tuple[RetrievedDocument, ...]:
        await self._ingest_pending()
        query_terms = set(_TOKEN.findall(query.text.lower()))
        query_vector: list[float] = []
        if query.mode in {RetrievalMode.VECTOR, RetrievalMode.HYBRID}:
            embedded = await self._embedder.embed([query.text])
            query_vector = embedded[0] if embedded else []
        keyword_weight, vector_weight = query.normalized_weights
        results: list[RetrievedDocument] = []
        for document in self._documents:
            if document.metadata.get("tenant_id") not in {None, query.tenant_id}:
                continue
            if any(document.metadata.get(key) != value for key, value in query.filters.items()):
                continue
            words = set(_TOKEN.findall(document.text.lower()))
            keyword_score = len(query_terms & words) / max(1, len(query_terms | words))
            vector_score = max(0.0, self._cosine(query_vector, self._vectors.get(document.id, [])))
            score = keyword_weight * keyword_score + vector_weight * vector_score
            if score >= query.minimum_score and (score > 0 or not query.text.strip()):
                results.append(
                    document.model_copy(
                        update={
                            "score": min(1.0, score),
                            "keyword_score": keyword_score,
                            "vector_score": vector_score,
                        }
                    )
                )
        deduplicated = self._deduplicate(results)
        ranked: Sequence[RetrievedDocument] = sorted(
            deduplicated, key=lambda item: (-item.score, item.id)
        )
        if self._reranker:
            ranked = await self._reranker.rerank(query.text, ranked)
        return tuple(ranked[: query.limit])

    async def _ingest_pending(self) -> None:
        async with self._ingest_lock:
            async with self._lock:
                pending = tuple(self._pending)
                self._pending.clear()
            if pending:
                try:
                    await self.ingest(pending)
                except Exception:
                    async with self._lock:
                        self._pending[0:0] = pending
                    raise

    @staticmethod
    def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if not left_norm or not right_norm:
            return 0.0
        return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)

    @staticmethod
    def _deduplicate(
        documents: Sequence[RetrievedDocument],
    ) -> tuple[RetrievedDocument, ...]:
        selected: dict[tuple[str, str], RetrievedDocument] = {}
        for document in documents:
            key = (document.source, " ".join(document.text.lower().split()))
            existing = selected.get(key)
            if existing is None or document.score > existing.score:
                selected[key] = document
        return tuple(selected.values())
