from __future__ import annotations

from collections.abc import Sequence

from algen_agent_runtime.types.interfaces import VectorStore


class RetrieverRegistry:
    def __init__(self) -> None:
        self._items: dict[str, VectorStore] = {}

    def register(self, name: str, retriever: VectorStore) -> None:
        if name in self._items:
            raise ValueError(f"retriever {name!r} is already registered")
        self._items[name] = retriever

    def get(self, name: str) -> VectorStore:
        try:
            return self._items[name]
        except KeyError as exc:
            raise KeyError(f"retriever {name!r} is not registered") from exc

    def list(self) -> Sequence[str]:
        return tuple(sorted(self._items))
