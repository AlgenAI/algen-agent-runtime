from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.models.providers.adapters import GeminiProvider, GroqProvider, XAIProvider
from algen_agent_runtime.orchestration.container import SUPPORTED_PROVIDER_TYPES, build_container
from algen_agent_runtime.types.contracts import (
    ImageBlock,
    Message,
    ModelCapabilities,
    ModelRequest,
    Role,
    TextBlock,
)

CASES = [
    (
        GeminiProvider,
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "GEMINI_API_KEY",
        "gemini-flash-latest",
    ),
    (XAIProvider, "xai", "https://api.x.ai/v1", "XAI_API_KEY", "grok-4"),
    (GroqProvider, "groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai/gpt-oss-120b"),
]


class StaticSecrets:
    def __init__(self, expected: str) -> None:
        self.expected = expected

    async def get(self, reference: str) -> str:
        assert reference == f"env://{self.expected}"
        return "test-key"


def _completion(model: str) -> dict[str, Any]:
    return {
        "id": "r-1",
        "model": model,
        "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 1},
    }


@pytest.mark.parametrize(("adapter", "provider_id", "base_url", "env", "default_model"), CASES)
async def test_chat_contract(
    adapter: Any, provider_id: str, base_url: str, env: str, default_model: str
) -> None:
    seen: dict[str, Any] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_completion(default_model))

    provider = adapter(
        f"env://{env}",
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        secret_provider=StaticSecrets(env),
    )
    response = await provider.generate(
        ModelRequest(
            messages=(Message.text(Role.USER, "Say ok"),),
            response_schema={
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["a"],
            },
            max_output_tokens=50,
            extensions={"reasoning_effort": "low"},
        )
    )
    assert provider.provider_id == provider_id
    assert seen["url"] == f"{base_url}/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    assert seen["payload"]["model"] == default_model
    assert seen["payload"]["reasoning_effort"] == "low"
    assert seen["payload"]["response_format"]["type"] == "json_schema"
    assert response.message.text_content == "ok"
    assert response.usage.input_tokens == 9


async def test_gemini_vision_and_embeddings() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/embeddings"):
            payload = json.loads(request.content)
            assert payload["model"] == "gemini-embedding-001"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": i, "embedding": [0.1, 0.2]}
                        for i, _ in enumerate(payload["input"])
                    ]
                },
            )
        payload = json.loads(request.content)
        assert payload["messages"][0]["content"][1]["type"] == "image_url"
        return httpx.Response(200, json=_completion("gemini-flash-latest"))

    provider = GeminiProvider(
        "env://GEMINI_API_KEY",
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        secret_provider=StaticSecrets("GEMINI_API_KEY"),
    )
    await provider.generate(
        ModelRequest(
            messages=(
                Message(
                    role=Role.USER,
                    content=(
                        TextBlock(text="Describe"),
                        ImageBlock(data="aGk=", media_type="image/png"),
                    ),
                ),
            ),
            max_output_tokens=20,
        )
    )
    assert await provider.embed(["a", "b"]) == [[0.1, 0.2], [0.1, 0.2]]
    embedding_caps = await provider.capabilities("gemini-embedding-001")
    assert embedding_caps.embeddings and not embedding_caps.chat


async def test_groq_and_xai_do_not_claim_embeddings() -> None:
    for adapter, env in ((GroqProvider, "GROQ_API_KEY"), (XAIProvider, "XAI_API_KEY")):
        caps = await adapter(f"env://{env}").capabilities("any")
        assert not caps.embeddings


def test_types_build_through_the_container(monkeypatch: pytest.MonkeyPatch) -> None:
    assert {"gemini", "xai", "groq"} <= SUPPORTED_PROVIDER_TYPES
    settings = AppSettings.model_validate(
        {
            "providers": {
                "g": {"type": "gemini", "default_model": "gemini-flash-latest"},
                "x": {
                    "type": "xai",
                    "default_model": "grok-4",
                    "base_url": "https://xai.example.test/v1",
                },
                "q": {
                    "type": "groq",
                    "default_model": "openai/gpt-oss-20b",
                    "capabilities": {"chat": True, "streaming": True, "tools": False},
                },
            },
            "agents": [
                {
                    "name": "hosted-check",
                    "version": "1.0.0",
                    "description": "d",
                    "system_instructions": "s",
                    "default_model": {"name": "p", "provider": "g", "model": "gemini-flash-latest"},
                }
            ],
        }
    )
    container = build_container(settings)
    router = container.runtime.router
    assert router.provider("g").provider_id == "gemini"
    assert router.provider("x").provider_id == "xai"
    groq = router.provider("q")
    assert groq.provider_id == "groq"
    assert groq._capabilities == ModelCapabilities(chat=True, streaming=True, tools=False)  # type: ignore[attr-defined]


async def test_gemini_merges_system_messages_into_one() -> None:
    """Gemini's OpenAI-compatible endpoint drops all but the last system message, which would lose
    agent instructions whenever retrieval adds its evidence as a second system message."""
    seen: dict[str, Any] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen["messages"] = json.loads(request.content)["messages"]
        return httpx.Response(200, json=_completion("gemini-flash-latest"))

    provider = GeminiProvider(
        "env://GEMINI_API_KEY",
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        secret_provider=StaticSecrets("GEMINI_API_KEY"),
    )
    await provider.generate(
        ModelRequest(
            messages=(
                Message.text(Role.SYSTEM, "You are a tutor."),
                Message.text(Role.SYSTEM, "Retrieved evidence follows. [S1] ..."),
                Message.text(Role.USER, "Question?"),
            ),
            max_output_tokens=20,
        )
    )
    assert [m["role"] for m in seen["messages"]] == ["system", "user"]
    assert (
        seen["messages"][0]["content"] == "You are a tutor.\n\nRetrieved evidence follows. [S1] ..."
    )
