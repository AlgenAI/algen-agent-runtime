from __future__ import annotations

import json

import httpx
import pytest

from algen_agent_runtime.exceptions.errors import ProviderError
from algen_agent_runtime.models.providers.adapters import MistralProvider
from algen_agent_runtime.types.contracts import (
    ErrorKind,
    FinishReason,
    ImageBlock,
    Message,
    ModelRequest,
    Role,
    TextBlock,
    ToolSpec,
)


class StaticSecrets:
    async def get(self, reference: str) -> str:
        assert reference == "env://MISTRAL_API_KEY"
        return "mistral-test-key"


async def test_mistral_chat_tools_structured_output_and_vision_contract() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.mistral.ai/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer mistral-test-key"
        payload = json.loads(request.content)
        assert payload["messages"][0]["content"][1] == {
            "type": "image_url",
            "image_url": "https://example.com/chart.png",
        }
        assert payload["tools"][0]["function"]["name"] == "lookup"
        assert payload["response_format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json={
                "id": "mistral-response-1",
                "model": "mistral-small-latest",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"id":7}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
        )

    provider = MistralProvider(
        "env://MISTRAL_API_KEY",
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        secret_provider=StaticSecrets(),
    )
    response = await provider.generate(
        ModelRequest(
            messages=(
                Message(
                    role=Role.USER,
                    content=(
                        TextBlock(text="Read the chart"),
                        ImageBlock(url="https://example.com/chart.png"),
                    ),
                ),
            ),
            tools=(
                ToolSpec(
                    name="lookup",
                    description="Look up an item.",
                    input_schema={
                        "type": "object",
                        "properties": {"id": {"type": "integer"}},
                        "required": ["id"],
                    },
                ),
            ),
            response_schema={
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        )
    )
    assert response.provider == "mistral"
    assert response.finish_reason == FinishReason.TOOL_CALLS
    assert response.tool_calls[0].arguments == {"id": 7}
    assert response.usage.total_tokens == 16


async def test_mistral_embeddings_contract() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.mistral.ai/v1/embeddings"
        assert json.loads(request.content) == {
            "model": "mistral-embed",
            "input": ["alpha", "beta"],
        }
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 0, "embedding": [0.1, 0.2]},
                    {"index": 1, "embedding": [0.3, 0.4]},
                ]
            },
        )

    provider = MistralProvider(
        "env://MISTRAL_API_KEY",
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        secret_provider=StaticSecrets(),
    )
    assert await provider.embed(("alpha", "beta")) == [
        [0.1, 0.2],
        [0.3, 0.4],
    ]
    capabilities = await provider.capabilities("mistral-embed")
    assert capabilities.embeddings is True
    assert capabilities.chat is False


async def test_mistral_stream_normalizes_deltas_tool_calls_and_usage() -> None:
    stream = "\n".join(
        (
            'data: {"id":"stream-1","model":"mistral-small-latest","choices":[{"delta":{"content":"Checking "},"finish_reason":null}]}',
            'data: {"id":"stream-1","model":"mistral-small-latest","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-9","function":{"name":"look","arguments":"{\\"id\\":"}}]},"finish_reason":null}]}',
            'data: {"id":"stream-1","model":"mistral-small-latest","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"up","arguments":"9}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":8,"completion_tokens":3}}',
            "data: [DONE]",
            "",
        )
    )

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    provider = MistralProvider(
        "env://MISTRAL_API_KEY",
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        secret_provider=StaticSecrets(),
    )
    events = [
        event
        async for event in provider.stream(
            ModelRequest(
                messages=(Message.text(Role.USER, "Check item 9"),),
                stream=True,
            )
        )
    ]
    assert events[0].delta == "Checking "
    assert events[-2].tool_call is not None
    assert events[-2].tool_call.name == "lookup"
    assert events[-2].tool_call.arguments == {"id": 9}
    assert events[-1].response is not None
    assert events[-1].response.usage.total_tokens == 11


async def test_mistral_error_preserves_safe_provider_diagnostics() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "object": "error",
                "message": "Workspace is not permitted to use this model",
                "type": "authorization_error",
                "code": "model_access_denied",
                "param": "model",
            },
        )

    provider = MistralProvider(
        "env://MISTRAL_API_KEY",
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        secret_provider=StaticSecrets(),
    )
    with pytest.raises(ProviderError) as captured:
        await provider.generate(ModelRequest(messages=(Message.text(Role.USER, "Hello"),)))

    assert captured.value.error_kind == ErrorKind.AUTHORIZATION
    assert captured.value.retryable is False
    assert "Workspace is not permitted" in str(captured.value)
    assert "code=model_access_denied" in str(captured.value)
    assert "mistral-test-key" not in str(captured.value)


async def test_mistral_normalizes_object_arguments_and_content_blocks() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "mistral-response-object-arguments",
                "model": "ministral-3b-2512",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": [{"type": "text", "text": "Running query"}],
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "analytics.query",
                                        "arguments": {"statement": "SELECT name FROM customers"},
                                    }
                                }
                            ],
                        },
                    }
                ],
            },
        )

    provider = MistralProvider(
        "env://MISTRAL_API_KEY",
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        secret_provider=StaticSecrets(),
    )
    response = await provider.generate(
        ModelRequest(messages=(Message.text(Role.USER, "List customers"),))
    )

    assert response.message.text_content == "Running query"
    assert response.tool_calls[0].id == "call-0"
    assert response.tool_calls[0].arguments == {"statement": "SELECT name FROM customers"}
