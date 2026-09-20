from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

from algen_agent_runtime.exceptions.errors import CapabilityError
from algen_agent_runtime.types.contracts import (
    Message,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    Role,
)


class LocalTransformersProvider:
    provider_id = "local_transformers"

    def __init__(self, model: str) -> None:
        self._model = model
        self._pipeline: Any = None

    async def _load(self) -> Any:
        if self._pipeline is None:
            try:
                from transformers import pipeline
            except ImportError as exc:
                raise CapabilityError(
                    "install algen-agent-runtime[transformers] for local Transformers"
                ) from exc
            self._pipeline = await asyncio.to_thread(pipeline, "text-generation", model=self._model)
        return self._pipeline

    async def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(chat=True)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        generator = await self._load()
        prompt = "\n".join(f"{m.role}: {m.text_content}" for m in request.messages)
        result: list[dict[str, Any]] = await asyncio.to_thread(
            generator, prompt, max_new_tokens=request.max_output_tokens or 256
        )
        text = result[0]["generated_text"][len(prompt) :]
        return ModelResponse(
            message=Message.text(Role.ASSISTANT, text), model=self._model, provider=self.provider_id
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        response = await self.generate(request)
        yield ModelStreamEvent(type="completed", response=response)

    async def embed(self, texts: Sequence[str], model: str | None = None) -> list[list[float]]:
        raise CapabilityError("this local Transformers adapter does not expose embeddings")

    async def health(self) -> bool:
        return True
