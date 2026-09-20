from __future__ import annotations

from dataclasses import dataclass

from algen_agent_runtime.retrieval.contracts import RetrievedDocument, SourceDocument


@dataclass(frozen=True)
class TextChunker:
    """Deterministic, dependency-free text chunker with bounded overlap."""

    chunk_size: int = 1_200
    chunk_overlap: int = 200

    def __post_init__(self) -> None:
        if self.chunk_size < 64:
            raise ValueError("chunk_size must be at least 64 characters")
        if self.chunk_overlap < 0 or self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be non-negative and smaller than chunk_size")

    def chunk(self, document: SourceDocument) -> tuple[RetrievedDocument, ...]:
        text = " ".join(document.text.split())
        chunks: list[RetrievedDocument] = []
        start = 0
        index = 0
        while start < len(text):
            hard_end = min(len(text), start + self.chunk_size)
            end = hard_end
            if hard_end < len(text):
                boundary = text.rfind(" ", start + self.chunk_size // 2, hard_end)
                if boundary > start:
                    end = boundary
            chunk_text = text[start:end].strip()
            if chunk_text:
                chunks.append(
                    RetrievedDocument(
                        id=f"{document.id}:{index}",
                        document_id=document.id,
                        text=chunk_text,
                        source=document.source,
                        title=document.title,
                        uri=document.uri,
                        chunk_index=index,
                        score=0,
                        metadata=document.metadata,
                    )
                )
                index += 1
            if end >= len(text):
                break
            start = max(start + 1, end - self.chunk_overlap)
        return tuple(chunks)
