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


GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
XAI_BASE_URL = "https://api.x.ai/v1"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"


def _system_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(
            text
            for text in (
                str(block.get("text") or "") for block in content if isinstance(block, dict)
            )
            if text
        )
    return ""


class GeminiProvider(OpenAICompatibleProvider):
    """Google Gemini through the Gemini API's OpenAI-compatible endpoint.

    Chat, streaming, tools, JSON-schema output, image input and embeddings. Thinking models accept
    ``reasoning_effort`` as a profile extension.
    """

    def __init__(
        self,
        api_key_reference: str,
        default_model: str = "gemini-flash-latest",
        capabilities: ModelCapabilities | None = None,
        base_url: str = GEMINI_BASE_URL,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            "gemini",
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
        # The OpenAI-compatible endpoint keeps only the last system message as the system
        # instruction. Runtime sends agent instructions, retrieved evidence and pinned compaction
        # summaries as a leading run of system messages, so merge that run, in order, into one.
        # A later system message (such as a verification-repair instruction) is sent in place as
        # a user turn so it keeps its position in the conversation.
        messages = payload["messages"]
        leading = 0
        while leading < len(messages) and messages[leading].get("role") == "system":
            leading += 1
        merged = "\n\n".join(
            text for text in (_system_text(m.get("content")) for m in messages[:leading]) if text
        )
        rewritten: list[dict[str, Any]] = [{"role": "system", "content": merged}] if merged else []
        for message in messages[leading:]:
            if message.get("role") == "system":
                text = _system_text(message.get("content"))
                if text:
                    rewritten.append({"role": "user", "content": f"[system] {text}"})
            else:
                rewritten.append(message)
        payload["messages"] = rewritten
        return payload

    async def capabilities(self, model: str) -> ModelCapabilities:
        if "embedding" in model:
            return ModelCapabilities(chat=False, embeddings=True)
        return await super().capabilities(model)

    async def embed(self, texts: Sequence[str], model: str | None = None) -> list[list[float]]:
        return await super().embed(texts, model or "gemini-embedding-001")


class XAIProvider(OpenAICompatibleProvider):
    """xAI Grok models through xAI's OpenAI-compatible API."""

    def __init__(
        self,
        api_key_reference: str,
        default_model: str = "grok-4",
        capabilities: ModelCapabilities | None = None,
        base_url: str = XAI_BASE_URL,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            "xai",
            base_url,
            api_key_reference,
            default_model,
            capabilities
            or ModelCapabilities(
                streaming=True, tools=True, structured_output=True, json_schema=True, images=True
            ),
            **kwargs,
        )


def _groq_strict_schema(model: str) -> bool:
    """Groq enforces ``strict`` JSON-schema output only for the gpt-oss family."""
    return model.startswith("openai/gpt-oss")


class GroqProvider(OpenAICompatibleProvider):
    """Models served by Groq's OpenAI-compatible API (for example ``openai/gpt-oss-120b``).

    Strict JSON-schema output is model-dependent on Groq: gpt-oss models report ``json_schema`` and
    get ``strict: true``; other models report only best-effort ``structured_output`` and are sent
    ``strict: false``. Configured ``capabilities`` replace this per-model default.
    """

    def __init__(
        self,
        api_key_reference: str,
        default_model: str = "openai/gpt-oss-120b",
        capabilities: ModelCapabilities | None = None,
        base_url: str = GROQ_BASE_URL,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            "groq",
            base_url,
            api_key_reference,
            default_model,
            capabilities
            or ModelCapabilities(
                streaming=True, tools=True, structured_output=True, json_schema=True
            ),
            **kwargs,
        )
        self._configured_capabilities = capabilities is not None

    def _strict_schema(self, model: str) -> bool:
        if self._configured_capabilities:
            return self._capabilities.json_schema
        return _groq_strict_schema(model)

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        payload = super()._payload(request)
        response_format = payload.get("response_format")
        if response_format and response_format.get("type") == "json_schema":
            response_format["json_schema"]["strict"] = self._strict_schema(payload["model"])
        return payload

    async def capabilities(self, model: str) -> ModelCapabilities:
        if self._configured_capabilities:
            return self._capabilities
        return self._capabilities.model_copy(
            update={"json_schema": self._strict_schema(model or self._default_model)}
        )
