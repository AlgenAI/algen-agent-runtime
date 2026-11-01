"""A malformed or cut-off tool call is a retryable model error, not a crashed run (#89)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.exceptions.errors import InvalidToolCallError
from algen_agent_runtime.models.providers.anthropic import AnthropicProvider
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.models.providers.openai_compatible import OpenAICompatibleProvider
from algen_agent_runtime.types.contracts import (
    ErrorKind,
    FinishReason,
    Message,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    RetryPolicy,
    Role,
    RunRequest,
)

SECRET_ARGUMENT = "ana@contoso.com"
MALFORMED = '{"to": "' + SECRET_ARGUMENT + '" "subject": "x"}'
CUT_OFF = '{"path": "notes.md", "content": "a long fi'
REQUEST = ModelRequest(messages=(Message.text(Role.USER, "q"),))


class _Secrets:
    async def get(self, reference: str) -> str:
        return "test-key"


def _transport(**response: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, **response))
    )


def _anthropic(**response: Any) -> AnthropicProvider:
    return AnthropicProvider(
        "env://ANTHROPIC_API_KEY",
        "claude-haiku-5-5",
        secret_provider=_Secrets(),
        client=_transport(**response),
    )


def _openai(**response: Any) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        "custom",
        "https://model.example/v1",
        None,
        "demo",
        ModelCapabilities(streaming=True, tools=True),
        client=_transport(**response),
    )


def _lines(events: list[dict[str, Any]], *, end: str = "") -> str:
    return "\n".join(f"data: {json.dumps(event)}" for event in events) + end


def _anthropic_stream(arguments: str, stop_reason: str) -> str:
    return _lines(
        [
            {"type": "message_start", "message": {"id": "m", "usage": {"input_tokens": 3}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "write_file"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": arguments},
            },
            {"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": {}},
        ]
    )


def _openai_stream(arguments: str, finish_reason: str) -> str:
    call = {"index": 0, "id": "call-1", "function": {"name": "write_file", "arguments": arguments}}
    return _lines(
        [
            {"choices": [{"delta": {"tool_calls": [call]}}]},
            {"choices": [{"delta": {}, "finish_reason": finish_reason}]},
        ],
        end="\ndata: [DONE]",
    )


async def _drain(provider: Any) -> None:
    async for _ in provider.stream(REQUEST.model_copy(update={"stream": True})):
        pass


def _assert_malformed(error: InvalidToolCallError, provider: str) -> None:
    assert error.error_kind is ErrorKind.INVALID_RESPONSE and error.retryable
    assert error.tool == "write_file" and not error.cut_off
    assert f"{provider} returned invalid arguments for tool 'write_file'" in str(error)
    assert SECRET_ARGUMENT not in str(error)


async def test_anthropic_streamed_malformed_call() -> None:
    provider = _anthropic(text=_anthropic_stream(MALFORMED, "tool_use"))
    with pytest.raises(InvalidToolCallError) as raised:
        await _drain(provider)
    _assert_malformed(raised.value, "anthropic")


async def test_anthropic_call_cut_off_by_max_tokens_says_so() -> None:
    provider = _anthropic(text=_anthropic_stream(CUT_OFF, "max_tokens"))
    with pytest.raises(InvalidToolCallError) as raised:
        await _drain(provider)
    assert raised.value.cut_off
    assert "cut off by the output-token limit (max_output_tokens)" in str(raised.value)


async def test_anthropic_non_object_input() -> None:
    reply = {
        "id": "m",
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "t", "name": "write_file", "input": ["x"]}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    with pytest.raises(InvalidToolCallError, match="not a JSON object"):
        await _anthropic(json=reply).generate(REQUEST)


@pytest.mark.parametrize("arguments", [MALFORMED, '["not", "an", "object"]'])
async def test_openai_non_streamed_invalid_arguments(arguments: str) -> None:
    call = {
        "id": "c",
        "type": "function",
        "function": {"name": "write_file", "arguments": arguments},
    }
    reply = {
        "id": "r",
        "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [call]}}],
    }
    with pytest.raises(InvalidToolCallError) as raised:
        await _openai(json=reply).generate(REQUEST)
    _assert_malformed(raised.value, "custom")


async def test_openai_streamed_malformed_and_cut_off_calls() -> None:
    with pytest.raises(InvalidToolCallError) as raised:
        await _drain(_openai(text=_openai_stream(MALFORMED, "tool_calls")))
    _assert_malformed(raised.value, "custom")

    with pytest.raises(InvalidToolCallError) as raised:
        await _drain(_openai(text=_openai_stream(CUT_OFF, "length")))
    assert raised.value.cut_off


def _malformed_once() -> MockModelProvider:
    calls = 0

    def handler(request: ModelRequest) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InvalidToolCallError("mock", "write_file", "Expecting ',' delimiter (char 43)")
        return ModelResponse(
            message=Message.text(Role.ASSISTANT, "done"),
            finish_reason=FinishReason.STOP,
            model="deterministic",
            provider="mock",
        )

    return MockModelProvider(handler=handler)


@pytest.mark.parametrize("stream", [False, True])
async def test_a_run_recovers_from_one_malformed_tool_call(stream: bool) -> None:
    provider = _malformed_once()
    runtime = make_runtime(
        provider, make_agent(retry_policy=RetryPolicy(initial_backoff_seconds=0))
    )

    result = await runtime.run(
        RunRequest(agent="test-agent", input="task", tenant_id="t", user_id="u", stream=stream)
    )

    assert result.status == "completed"
    assert result.output == "done"
    assert len(provider.requests) == 2
    audits = [
        event
        for event in await runtime.audits.list("t")
        if event.action == "model.tool_call_invalid"
    ]
    assert [(event.outcome, event.metadata["tool"]) for event in audits] == [
        ("malformed", "write_file")
    ]
    # The provider answered; a bad tool call does not open its circuit (threshold 1 here).
    assert await runtime.router.candidates((make_agent().default_model,), frozenset())
