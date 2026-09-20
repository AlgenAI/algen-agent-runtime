from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable, Sequence

from algen_agent_runtime.types.contracts import (
    FinishReason,
    Message,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    Role,
    TokenUsage,
    ToolCall,
)


class MockModelProvider:
    provider_id = "mock"

    def __init__(
        self,
        responses: Sequence[str | ModelResponse] = (),
        handler: Callable[[ModelRequest], ModelResponse] | None = None,
        delay_seconds: float = 0,
    ) -> None:
        self._responses = list(responses)
        self._handler = handler
        self._delay = delay_seconds
        self.requests: list[ModelRequest] = []

    async def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            streaming=True,
            tools=True,
            structured_output=True,
            json_schema=True,
            embeddings=True,
            images=True,
            files=True,
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._handler:
            return self._handler(request)
        item = self._responses.pop(0) if self._responses else request.messages[-1].text_content
        if isinstance(item, ModelResponse):
            return item
        return ModelResponse(
            message=Message.text(Role.ASSISTANT, item),
            finish_reason=FinishReason.STOP,
            usage=TokenUsage(input_tokens=10, output_tokens=max(1, len(item) // 4)),
            model=request.model or "deterministic",
            provider=self.provider_id,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        response = await self.generate(request)
        for word in response.message.text_content.split():
            yield ModelStreamEvent(type="delta", delta=word + " ")
            await asyncio.sleep(0)
        for call in response.tool_calls:
            yield ModelStreamEvent(type="tool_call", tool_call=call)
        yield ModelStreamEvent(type="completed", response=response)

    async def embed(self, texts: Sequence[str], model: str | None = None) -> list[list[float]]:
        return [
            [byte / 255 for byte in hashlib.sha256(text.encode()).digest()[:16]] for text in texts
        ]

    async def health(self) -> bool:
        return True


def tool_call_response(name: str, arguments: dict[str, object]) -> ModelResponse:
    call = ToolCall(name=name, arguments=arguments)
    return ModelResponse(
        message=Message.text(Role.ASSISTANT, ""),
        tool_calls=(call,),
        finish_reason=FinishReason.TOOL_CALLS,
        model="deterministic",
        provider="mock",
    )
