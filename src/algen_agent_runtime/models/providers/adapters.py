from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from algen_agent_runtime.models.providers.anthropic import (
    AnthropicProvider as AnthropicProvider,  # re-exported; the adapter lives in its own module
)
from algen_agent_runtime.models.providers.openai_compatible import (
    OpenAICompatibleProvider,
    openai_strict_json_schema,
)
from algen_agent_runtime.types.contracts import (
    ModelCapabilities,
    ModelRequest,
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
        if request.response_schema:
            payload["response_format"]["json_schema"]["schema"] = openai_strict_json_schema(
                request.response_schema
            )
        model = str(payload.get("model", "")).lower()
        uses_completion_limit = model.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))
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
        stream_usage: bool | None = None,
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
            stream_usage=stream_usage,
        )
        self._key_reference = api_key_reference

    async def _auth_headers(self) -> dict[str, str]:
        return {"api-key": await self._secrets.get(self._key_reference or "")}

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        payload = super()._payload(request)
        if request.response_schema:
            payload["response_format"]["json_schema"]["schema"] = openai_strict_json_schema(
                request.response_schema
            )
        return payload


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
    # Mistral's chat API is not OpenAI's; it reports usage without being asked.
    default_stream_usage = False

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
