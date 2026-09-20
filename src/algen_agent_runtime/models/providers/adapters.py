from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from algen_agent_runtime.exceptions.errors import ProviderError
from algen_agent_runtime.models.providers.openai_compatible import OpenAICompatibleProvider
from algen_agent_runtime.types.contracts import (
    ErrorKind,
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


class OpenAIProvider(OpenAICompatibleProvider):
    def __init__(self, api_key_reference: str, default_model: str, **kwargs: Any) -> None:
        super().__init__(
            "openai",
            "https://api.openai.com/v1",
            api_key_reference,
            default_model,
            ModelCapabilities(
                streaming=True,
                tools=True,
                structured_output=True,
                json_schema=True,
                embeddings=True,
                images=True,
                files=True,
            ),
            **kwargs,
        )

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        payload = super()._payload(request)
        model = str(payload.get("model", "")).lower()
        uses_completion_limit = model.startswith(("gpt-5", "o1", "o3", "o4"))
        if uses_completion_limit and "max_tokens" in payload:
            payload["max_completion_tokens"] = payload.pop("max_tokens")
        return payload


class AzureOpenAIProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        base_url: str,
        api_key_reference: str,
        deployment: str,
        api_version: str,
        secret_provider: Any | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            "azure_openai",
            base_url,
            None,
            deployment,
            ModelCapabilities(
                streaming=True,
                tools=True,
                structured_output=True,
                json_schema=True,
                embeddings=True,
                images=True,
            ),
            secret_provider,
            client,
            headers={"api-key": "resolved-at-request"},
            endpoint=f"/openai/deployments/{deployment}/chat/completions?api-version={api_version}",
        )
        self._key_reference = api_key_reference

    async def _auth_headers(self) -> dict[str, str]:
        return {"api-key": await self._secrets.get(self._key_reference or "")}


class AnthropicProvider(OpenAICompatibleProvider):
    """Native Anthropic Messages API adapter with normalized contracts."""

    def __init__(
        self,
        api_key_reference: str,
        default_model: str,
        base_url: str = "https://api.anthropic.com/v1",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            "anthropic",
            base_url,
            api_key_reference,
            default_model,
            ModelCapabilities(
                streaming=True, tools=True, structured_output=False, json_schema=False, images=True
            ),
            endpoint="/messages",
            **kwargs,
        )

    async def _auth_headers(self) -> dict[str, str]:
        return {
            "x-api-key": await self._secrets.get(self._key_reference or ""),
            "anthropic-version": "2023-06-01",
        }

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        system = "\n".join(
            message.text_content for message in request.messages if message.role == Role.SYSTEM
        )
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == Role.SYSTEM:
                continue
            if message.role == Role.TOOL:
                content: Any = [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": message.text_content,
                    }
                ]
                role = "user"
            else:
                role = message.role.value
                content = []
                for block in message.content:
                    if block.type == "text" and block.text:
                        content.append({"type": "text", "text": block.text})
                    elif block.type == "image":
                        if block.data:
                            content.append(
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": block.media_type,
                                        "data": block.data,
                                    },
                                }
                            )
                content.extend(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": self._wire_tool_name(call.name),
                        "input": call.arguments,
                    }
                    for call in message.tool_calls
                )
            messages.append({"role": role, "content": content})
        payload: dict[str, Any] = {
            "model": request.model or self._default_model,
            "messages": messages,
            "max_tokens": request.max_output_tokens or 1024,
            "stream": request.stream,
        }
        if system:
            payload["system"] = system
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = [
                {
                    "name": self._wire_tool_name(tool.name),
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in request.tools
            ]
        payload.update(request.extensions)
        return payload

    def _response(self, data: dict[str, Any], started: float, raw: bool) -> ModelResponse:
        text = "".join(
            item.get("text", "") for item in data.get("content", ()) if item.get("type") == "text"
        )
        calls = tuple(
            ToolCall(
                id=item["id"],
                name=self._runtime_tool_name(item["name"]),
                arguments=item.get("input", {}),
            )
            for item in data.get("content", ())
            if item.get("type") == "tool_use"
        )
        usage = data.get("usage") or {}
        return ModelResponse(
            id=data.get("id", "unknown"),
            message=Message.text(Role.ASSISTANT, text),
            tool_calls=calls,
            finish_reason=FinishReason.TOOL_CALLS if calls else FinishReason.STOP,
            usage=TokenUsage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            ),
            latency_ms=(time.monotonic() - started) * 1000,
            model=data.get("model", self._default_model),
            provider=self.provider_id,
            raw_metadata=data if raw else None,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        started = time.monotonic()
        response_id = "unknown"
        model = request.model or self._default_model
        text_parts: list[str] = []
        call_parts: dict[int, dict[str, Any]] = {}
        usage = TokenUsage()
        try:
            async with self._client.stream(
                "POST",
                self._base_url + self._endpoint,
                headers=await self._auth_headers(),
                json=self._payload(request.model_copy(update={"stream": True})),
                timeout=request.timeout_seconds,
            ) as response:
                if response.is_error:
                    retryable = response.status_code == 429 or response.status_code >= 500
                    raise ProviderError(
                        f"anthropic returned HTTP {response.status_code}",
                        ErrorKind.RATE_LIMIT
                        if response.status_code == 429
                        else ErrorKind.UNAVAILABLE,
                        retryable,
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = json.loads(line.removeprefix("data: "))
                    event_type = data.get("type")
                    if event_type == "message_start":
                        message = data.get("message", {})
                        response_id = message.get("id", response_id)
                        model = message.get("model", model)
                        usage = usage.model_copy(
                            update={"input_tokens": message.get("usage", {}).get("input_tokens", 0)}
                        )
                    elif event_type == "content_block_start":
                        block = data.get("content_block", {})
                        if block.get("type") == "tool_use":
                            call_parts[data["index"]] = {
                                "id": block["id"],
                                "name": self._runtime_tool_name(block["name"]),
                                "json": "",
                            }
                    elif event_type == "content_block_delta":
                        delta = data.get("delta", {})
                        if delta.get("type") == "text_delta":
                            piece = delta.get("text", "")
                            text_parts.append(piece)
                            yield ModelStreamEvent(type="delta", delta=piece)
                        elif delta.get("type") == "input_json_delta":
                            call_parts[data["index"]]["json"] += delta.get("partial_json", "")
                    elif event_type == "message_delta":
                        usage = usage.model_copy(
                            update={"output_tokens": data.get("usage", {}).get("output_tokens", 0)}
                        )
        except httpx.TimeoutException as exc:
            raise ProviderError("anthropic stream timed out", ErrorKind.TIMEOUT, True) from exc
        calls = tuple(
            ToolCall(
                id=item["id"],
                name=item["name"],
                arguments=json.loads(item["json"] or "{}"),
            )
            for _, item in sorted(call_parts.items())
        )
        for call in calls:
            yield ModelStreamEvent(type="tool_call", tool_call=call)
        completed = ModelResponse(
            id=response_id,
            message=Message.text(Role.ASSISTANT, "".join(text_parts)),
            tool_calls=calls,
            finish_reason=FinishReason.TOOL_CALLS if calls else FinishReason.STOP,
            usage=usage,
            latency_ms=(time.monotonic() - started) * 1000,
            model=model,
            provider=self.provider_id,
        )
        yield ModelStreamEvent(type="completed", response=completed)


class DeepSeekProvider(OpenAICompatibleProvider):
    def __init__(
        self, api_key_reference: str, default_model: str = "deepseek-chat", **kwargs: Any
    ) -> None:
        super().__init__(
            "deepseek",
            "https://api.deepseek.com/v1",
            api_key_reference,
            default_model,
            ModelCapabilities(
                streaming=True, tools=True, structured_output=True, json_schema=False
            ),
            **kwargs,
        )


class MistralProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        api_key_reference: str,
        default_model: str = "mistral-small-latest",
        capabilities: ModelCapabilities | None = None,
        base_url: str = "https://api.mistral.ai/v1",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            "mistral",
            base_url,
            api_key_reference,
            default_model,
            capabilities
            or ModelCapabilities(
                streaming=True,
                tools=True,
                structured_output=True,
                json_schema=True,
                embeddings=True,
                images=True,
            ),
            **kwargs,
        )

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        payload = super()._payload(request)
        for message in payload["messages"]:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                image_url = block.get("image_url")
                if block.get("type") == "image_url" and isinstance(image_url, dict):
                    block["image_url"] = image_url["url"]
        return payload

    async def capabilities(self, model: str) -> ModelCapabilities:
        if model.startswith("mistral-embed"):
            return ModelCapabilities(chat=False, embeddings=True)
        return await super().capabilities(model)

    async def embed(self, texts: Sequence[str], model: str | None = None) -> list[list[float]]:
        return await super().embed(texts, model or "mistral-embed")


class OllamaProvider(OpenAICompatibleProvider):
    def __init__(
        self, default_model: str, base_url: str = "http://127.0.0.1:11434/v1", **kwargs: Any
    ) -> None:
        super().__init__(
            "ollama",
            base_url,
            None,
            default_model,
            ModelCapabilities(
                streaming=True,
                tools=True,
                structured_output=True,
                json_schema=True,
                embeddings=True,
                images=True,
            ),
            **kwargs,
        )


class HuggingFaceInferenceProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        api_key_reference: str,
        default_model: str,
        base_url: str = "https://router.huggingface.co/v1",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            "huggingface_inference",
            base_url,
            api_key_reference,
            default_model,
            ModelCapabilities(
                streaming=True, tools=True, structured_output=False, json_schema=False
            ),
            **kwargs,
        )
