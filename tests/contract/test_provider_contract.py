import httpx

from algen_agent_runtime.models.providers.adapters import AnthropicProvider, OpenAIProvider
from algen_agent_runtime.models.providers.openai_compatible import OpenAICompatibleProvider
from algen_agent_runtime.types.contracts import (
    Message,
    ModelCapabilities,
    ModelRequest,
    Role,
    ToolSpec,
)


async def test_openai_compatible_normalizes_wire_response() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "response-1",
                "model": "demo",
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    provider = OpenAICompatibleProvider(
        "custom",
        "https://model.example/v1",
        None,
        "demo",
        ModelCapabilities(streaming=True),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    response = await provider.generate(ModelRequest(messages=(Message.text(Role.USER, "hi"),)))
    assert response.provider == "custom"
    assert response.message.text_content == "ok"
    assert response.usage.total_tokens == 4


async def test_openai_maps_namespaced_tool_names_at_provider_boundary() -> None:
    observed_name = ""

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal observed_name
        body = __import__("json").loads(request.content)
        observed_name = body["tools"][0]["function"]["name"]
        assert "." not in observed_name
        assert len(observed_name) <= 64
        return httpx.Response(
            200,
            json={
                "id": "response-tools",
                "model": "gpt-5-mini",
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
                                        "name": observed_name,
                                        "arguments": '{"days":10}',
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )

    provider = OpenAIProvider(
        "env://OPENAI_API_KEY",
        "gpt-5-mini",
        secret_provider=StaticSecrets(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    response = await provider.generate(
        ModelRequest(
            messages=(Message.text(Role.USER, "make a plan"),),
            tools=(
                ToolSpec(
                    name="teaching.build_study_plan",
                    description="Build a study plan",
                    input_schema={"type": "object"},
                ),
            ),
        )
    )

    assert observed_name.startswith("teaching_build_study_plan_")
    assert response.tool_calls[0].name == "teaching.build_study_plan"


async def test_openai_uses_modern_completion_token_parameter() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        assert body["max_completion_tokens"] == 256
        assert "max_tokens" not in body
        return httpx.Response(
            200,
            json={
                "id": "response-1",
                "model": "gpt-5",
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    provider = OpenAIProvider(
        "env://OPENAI_API_KEY",
        "gpt-5",
        secret_provider=StaticSecrets(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    response = await provider.generate(
        ModelRequest(
            messages=(Message.text(Role.USER, "hi"),),
            max_output_tokens=256,
        )
    )
    assert response.message.text_content == "ok"


async def test_openai_normalizes_nested_pydantic_schema_for_strict_outputs() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        schema = body["response_format"]["json_schema"]["schema"]
        assert schema["required"] == ["evidence", "ambiguities", "safe"]
        assert schema["additionalProperties"] is False
        assert "default" not in schema["properties"]["ambiguities"]
        item = schema["$defs"]["Evidence"]
        assert item["required"] == ["claim", "confidence"]
        assert item["additionalProperties"] is False
        assert "default" not in item["properties"]["confidence"]
        return httpx.Response(
            200,
            json={
                "id": "response-schema",
                "model": "gpt-4o",
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            },
        )

    provider = OpenAIProvider(
        "env://OPENAI_API_KEY",
        "gpt-4o",
        secret_provider=StaticSecrets(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    await provider.generate(
        ModelRequest(
            messages=(Message.text(Role.USER, "extract"),),
            response_schema={
                "$defs": {
                    "Evidence": {
                        "type": "object",
                        "properties": {
                            "claim": {"type": "string"},
                            "confidence": {"type": "number", "default": 0.5},
                        },
                    }
                },
                "type": "object",
                "properties": {
                    "evidence": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/Evidence"},
                    },
                    "ambiguities": {"type": "array", "items": {"type": "string"}, "default": []},
                    "safe": {"type": "boolean", "default": True},
                },
            },
        )
    )


async def test_openai_preserves_legacy_token_parameter_for_older_models() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        assert body["max_tokens"] == 128
        assert "max_completion_tokens" not in body
        return httpx.Response(
            200,
            json={
                "id": "response-legacy",
                "model": "gpt-4o",
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
            },
        )

    provider = OpenAIProvider(
        "env://OPENAI_API_KEY",
        "gpt-4o",
        secret_provider=StaticSecrets(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    await provider.generate(
        ModelRequest(
            messages=(Message.text(Role.USER, "hi"),),
            max_output_tokens=128,
        )
    )


class StaticSecrets:
    async def get(self, reference: str) -> str:
        return "test-key"


async def test_anthropic_native_messages_contract() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "test-key"
        body = __import__("json").loads(request.content)
        assert body["system"] == "system"
        return httpx.Response(
            200,
            json={
                "id": "msg-1",
                "model": "claude-test",
                "content": [
                    {"type": "text", "text": "checking"},
                    {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {"id": 1}},
                ],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        )

    provider = AnthropicProvider(
        "env://ANTHROPIC_API_KEY",
        "claude-test",
        secret_provider=StaticSecrets(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    response = await provider.generate(
        ModelRequest(
            messages=(Message.text(Role.SYSTEM, "system"), Message.text(Role.USER, "find"))
        )
    )
    assert response.tool_calls[0].name == "lookup"
    assert response.usage.total_tokens == 6
