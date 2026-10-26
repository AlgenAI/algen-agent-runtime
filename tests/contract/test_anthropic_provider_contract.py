from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.models.providers.anthropic import (
    AnthropicProvider,
    anthropic_json_schema,
)
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.types.contracts import (
    FinishReason,
    Message,
    ModelRequest,
    Role,
    ToolCall,
)


class _Secrets:
    async def get(self, reference: str) -> str:
        return "test-key"


def _provider(
    reply: dict[str, Any] | None = None,
    sent: list[dict[str, Any]] | None = None,
    *,
    stream_lines: list[str] | None = None,
    prompt_caching: bool = True,
) -> AnthropicProvider:
    def respond(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append(json.loads(request.content))
        if stream_lines is not None:
            return httpx.Response(200, text="\n".join(stream_lines))
        return httpx.Response(200, json=reply or _reply())

    return AnthropicProvider(
        "env://ANTHROPIC_API_KEY",
        "claude-sonnet-5-5",
        secret_provider=_Secrets(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        prompt_caching=prompt_caching,
    )


def _reply(**updates: Any) -> dict[str, Any]:
    return {
        "id": "msg-1",
        "model": "claude-sonnet-5-5",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": '{"score": 3}'}],
        "usage": {"input_tokens": 10, "output_tokens": 4},
        **updates,
    }


_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 4},
        "summary": {"type": "string", "maxLength": 400, "format": "markdown"},
        "decided_at": {"type": "string", "format": "date-time"},
        "evidence": {
            "type": "array",
            "maxItems": 5,
            "items": {"type": "object", "properties": {"quote": {"type": "string"}}},
        },
    },
    "required": ["score"],
}


async def test_provider_declares_native_structured_output() -> None:
    capabilities = await _provider().capabilities("claude-sonnet-5-5")

    assert capabilities.structured_output is True
    assert capabilities.json_schema is True


def test_schema_translation_drops_unsupported_constraints_and_closes_objects() -> None:
    translated = anthropic_json_schema(_SCHEMA)

    assert translated["additionalProperties"] is False
    assert translated["properties"]["score"] == {"type": "integer"}
    assert translated["properties"]["summary"] == {"type": "string"}
    assert translated["properties"]["decided_at"]["format"] == "date-time"
    evidence = translated["properties"]["evidence"]
    assert "maxItems" not in evidence
    assert evidence["items"]["additionalProperties"] is False
    assert translated["required"] == ["score"]
    assert _SCHEMA["properties"]["score"]["minimum"] == 0  # the caller's schema is untouched


async def test_response_schema_is_sent_as_output_config_format() -> None:
    sent: list[dict[str, Any]] = []

    response = await _provider(sent=sent).generate(
        ModelRequest(messages=(Message.text(Role.USER, "score"),), response_schema=_SCHEMA)
    )

    assert sent[0]["output_config"] == {
        "format": {"type": "json_schema", "schema": anthropic_json_schema(_SCHEMA)}
    }
    assert response.message.text_content == '{"score": 3}'


async def test_profile_output_config_merges_with_the_response_format() -> None:
    sent: list[dict[str, Any]] = []

    await _provider(sent=sent).generate(
        ModelRequest(
            messages=(Message.text(Role.USER, "score"),),
            response_schema=_SCHEMA,
            extensions={"output_config": {"effort": "low"}, "metadata": {"user_id": "u"}},
        )
    )

    assert sent[0]["output_config"]["effort"] == "low"
    assert sent[0]["output_config"]["format"]["type"] == "json_schema"
    assert sent[0]["metadata"] == {"user_id": "u"}


async def test_parallel_tool_results_share_one_user_turn() -> None:
    sent: list[dict[str, Any]] = []
    calls = (
        ToolCall(id="a", name="lookup", arguments={"id": 1}),
        ToolCall(id="b", name="lookup", arguments={"id": 2}),
    )
    messages = (
        Message.text(Role.USER, "find both"),
        Message(role=Role.ASSISTANT, content=(), tool_calls=calls),
        Message.text(Role.TOOL, '{"found": 1}').model_copy(update={"tool_call_id": "a"}),
        Message.text(Role.TOOL, '{"found": 2}').model_copy(update={"tool_call_id": "b"}),
        Message.text(Role.USER, "thanks"),
    )

    await _provider(sent=sent).generate(ModelRequest(messages=messages))

    wire = sent[0]["messages"]
    assert [turn["role"] for turn in wire] == ["user", "assistant", "user", "user"]
    assert [block["tool_use_id"] for block in wire[2]["content"]] == ["a", "b"]
    assert wire[3]["content"] == [{"type": "text", "text": "thanks"}]


async def test_thinking_blocks_are_never_surfaced() -> None:
    reply = _reply(
        content=[
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": "answer"},
        ]
    )

    response = await _provider(reply).generate(
        ModelRequest(messages=(Message.text(Role.USER, "q"),))
    )

    assert response.message.text_content == "answer"


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", FinishReason.STOP),
        ("max_tokens", FinishReason.LENGTH),
        ("refusal", FinishReason.CONTENT_FILTER),
        ("something_new", FinishReason.STOP),
    ],
)
async def test_stop_reasons_map_to_finish_reasons(stop_reason: str, expected: FinishReason) -> None:
    response = await _provider(_reply(stop_reason=stop_reason)).generate(
        ModelRequest(messages=(Message.text(Role.USER, "q"),))
    )

    assert response.finish_reason == expected


async def test_streamed_stop_reason_is_reported() -> None:
    events = [
        {"type": "message_start", "message": {"id": "m", "usage": {"input_tokens": 3}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "max_tokens"},
            "usage": {"output_tokens": 9},
        },
    ]
    provider = _provider(stream_lines=[f"data: {json.dumps(event)}" for event in events])

    streamed = [
        event
        async for event in provider.stream(
            ModelRequest(messages=(Message.text(Role.USER, "q"),), stream=True)
        )
    ]

    completed = streamed[-1].response
    assert completed is not None
    assert completed.finish_reason == FinishReason.LENGTH
    assert completed.usage.output_tokens == 9


_CONVERSATION = (
    Message.text(Role.SYSTEM, "You are a careful assistant."),
    Message.text(Role.USER, "q"),
)


async def test_prompt_caching_marks_the_instructions_and_the_conversation() -> None:
    sent: list[dict[str, Any]] = []
    await _provider(sent=sent).generate(ModelRequest(messages=_CONVERSATION))

    [body] = sent
    # Tools and instructions are read back on every call; the tail moves with the conversation.
    assert body["system"] == [
        {
            "type": "text",
            "text": "You are a careful assistant.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert body["cache_control"] == {"type": "ephemeral"}


async def test_prompt_caching_can_be_turned_off_for_a_gateway() -> None:
    sent: list[dict[str, Any]] = []
    await _provider(sent=sent, prompt_caching=False).generate(ModelRequest(messages=_CONVERSATION))

    [body] = sent
    assert body["system"] == "You are a careful assistant." and "cache_control" not in body


async def test_usage_counts_the_whole_prompt_and_reports_what_was_cached() -> None:
    usage = {
        "input_tokens": 50,
        "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 9_000,
        "output_tokens": 7,
    }
    response = await _provider(_reply(usage=usage)).generate(ModelRequest(messages=_CONVERSATION))

    # Anthropic's input_tokens is only the uncached remainder; budgets and credits need it all.
    assert response.usage.input_tokens == 9_250
    assert response.usage.cached_tokens == 9_000
    assert response.usage.output_tokens == 7


async def test_streamed_usage_counts_the_whole_prompt() -> None:
    start_usage = {
        "input_tokens": 5,
        "cache_creation_input_tokens": 40,
        "cache_read_input_tokens": 3_000,
    }
    events = [
        {"type": "message_start", "message": {"id": "m", "usage": start_usage}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 2},
        },
    ]
    provider = _provider(stream_lines=[f"data: {json.dumps(event)}" for event in events])

    streamed = [
        event async for event in provider.stream(ModelRequest(messages=_CONVERSATION, stream=True))
    ]

    completed = streamed[-1].response
    assert completed is not None
    assert (completed.usage.input_tokens, completed.usage.cached_tokens) == (3_045, 3_000)
    assert completed.usage.output_tokens == 2


@pytest.mark.parametrize(("configured", "expected"), [(None, True), (False, False)])
def test_prompt_caching_setting_reaches_the_adapter(
    configured: bool | None, expected: bool
) -> None:
    entry = {"type": "anthropic", "default_model": "claude-haiku-5-5"}
    settings = AppSettings.model_validate(
        {
            "providers": {
                "claude": {
                    **entry,
                    **({} if configured is None else {"prompt_caching": configured}),
                }
            }
        }
    )

    provider = build_container(settings).runtime.router._providers["claude"]

    assert isinstance(provider, AnthropicProvider)
    assert provider._prompt_caching is expected
