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
    ToolCall,
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


def _gemini_capturing(seen: dict[str, Any]) -> GeminiProvider:
    def respond(request: httpx.Request) -> httpx.Response:
        seen["messages"] = json.loads(request.content)["messages"]
        return httpx.Response(200, json=_completion("gemini-flash-latest"))

    return GeminiProvider(
        "env://GEMINI_API_KEY",
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        secret_provider=StaticSecrets("GEMINI_API_KEY"),
    )


async def test_gemini_keeps_a_later_system_message_in_place() -> None:
    """Only the leading system run is merged. The verification-repair instruction Runtime appends
    after a rejected answer stays last, as a user turn, so "the previous response" still follows it.
    """
    seen: dict[str, Any] = {}
    provider = _gemini_capturing(seen)
    await provider.generate(
        ModelRequest(
            messages=(
                Message.text(Role.SYSTEM, "Always end with BANANA."),
                Message.text(Role.SYSTEM, "Evidence: [S1] ..."),
                Message.text(Role.USER, "Q?"),
                Message(
                    role=Role.ASSISTANT,
                    content=(),
                    tool_calls=(ToolCall(id="c1", name="search", arguments={"q": "x"}),),
                ),
                Message.text(Role.TOOL, "result").model_copy(update={"tool_call_id": "c1"}),
                Message.text(Role.ASSISTANT, "bad answer"),
                Message.text(Role.SYSTEM, "Repair the previous response to satisfy validation."),
            ),
            max_output_tokens=20,
        )
    )
    messages = seen["messages"]
    assert [m["role"] for m in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert messages[0]["content"] == "Always end with BANANA.\n\nEvidence: [S1] ..."
    assert messages[2]["tool_calls"][0]["id"] == "c1"
    assert messages[3]["tool_call_id"] == "c1"
    assert messages[-1] == {
        "role": "user",
        "content": "[system] Repair the previous response to satisfy validation.",
    }


async def test_gemini_system_text_joins_blocks_and_skips_empty_system() -> None:
    seen: dict[str, Any] = {}
    provider = _gemini_capturing(seen)
    await provider.generate(
        ModelRequest(
            messages=(
                Message(role=Role.SYSTEM, content=(TextBlock(text="One."), TextBlock(text="Two."))),
                Message.text(Role.USER, "Q?"),
            ),
            max_output_tokens=20,
        )
    )
    assert seen["messages"][0] == {"role": "system", "content": "One.\n\nTwo."}

    await provider.generate(
        ModelRequest(
            messages=(Message.text(Role.SYSTEM, ""), Message.text(Role.USER, "Q?")),
            max_output_tokens=20,
        )
    )
    assert [m["role"] for m in seen["messages"]] == ["user"]


async def test_groq_strict_json_schema_is_model_dependent() -> None:
    seen: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_completion("m"))

    def groq(capabilities: ModelCapabilities | None = None) -> GroqProvider:
        return GroqProvider(
            "env://GROQ_API_KEY",
            capabilities=capabilities,
            client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
            secret_provider=StaticSecrets("GROQ_API_KEY"),
        )

    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}

    def request(model: str) -> ModelRequest:
        return ModelRequest(
            messages=(Message.text(Role.USER, "x"),),
            model=model,
            response_schema=schema,
            max_output_tokens=20,
        )

    provider = groq()
    await provider.generate(request("openai/gpt-oss-20b"))
    await provider.generate(request("llama-3.3-70b-versatile"))
    assert [p["response_format"]["json_schema"]["strict"] for p in seen] == [True, False]
    assert (await provider.capabilities("openai/gpt-oss-120b")).json_schema
    llama = await provider.capabilities("llama-3.3-70b-versatile")
    assert llama.structured_output and not llama.json_schema

    # Configured capabilities replace the per-model default.
    seen.clear()
    configured = groq(ModelCapabilities(streaming=True, structured_output=True, json_schema=True))
    await configured.generate(request("llama-3.3-70b-versatile"))
    assert seen[0]["response_format"]["json_schema"]["strict"] is True
    assert (await configured.capabilities("llama-3.3-70b-versatile")).json_schema
