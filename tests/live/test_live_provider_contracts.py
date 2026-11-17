"""Live provider contracts: every capability a provider class claims, checked against the API.

Run before every release with real keys (``ALGEN_LIVE=1``; see docs/releasing.md, "Live
provider contracts"), or offline against the local fake API (``ALGEN_LIVE_FAKE=1``) to check the
suite itself. Without either, every test here is collected and skipped.

Each test drives the Runtime's own provider class (and ``ModelRouter``, ``build_container`` and
``Runtime.run``), never raw HTTP. A capability test runs when the class claims the capability
(``await provider.capabilities(model)``) and is skipped, saying so, when it does not: a
capability that is claimed but that the live API does not honour fails the suite.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass, field
from typing import Any

import httpx
import jsonschema
import pytest
from fake_model_server import TrackingTransport
from live_fake_api import FAKE_KEY, SLOW_PROMPT, UNKNOWN_MODEL, LiveFakeAPI
from live_support import (
    BY_NAME,
    FAKE,
    FORECAST_CODE,
    HANG_UP_SECONDS,
    LIVE,
    MAX_OUTPUT_TOKENS,
    PDF_CODE,
    SKIP_REASON,
    TOOL_NAME,
    WRONG_KEY,
    LiveSecrets,
    Target,
    WrongKeySecrets,
    azure_api_version,
    build_provider,
    code_pdf,
    ollama_base_url,
    red_png,
    selected,
    unavailable,
)

from algen_agent_runtime.exceptions.errors import ProviderError
from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.metering import MeterCall
from algen_agent_runtime.models.providers.anthropic import AnthropicProvider
from algen_agent_runtime.models.providers.openai_compatible import OpenAICompatibleProvider
from algen_agent_runtime.types.contracts import (
    ErrorKind,
    FileBlock,
    FinishReason,
    ImageBlock,
    Message,
    ModelCapabilities,
    ModelProfile,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    Role,
    TextBlock,
    TokenUsage,
    ToolSpec,
)

pytestmark = [pytest.mark.live, pytest.mark.skipif(not (FAKE or LIVE), reason=SKIP_REASON)]

COST_IN, COST_OUT = 1.0, 2.0  # per 1k tokens: any non-zero price shows the router prices calls

FORECAST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"city": {"type": "string", "description": "The city name"}},
    "required": ["city"],
    "additionalProperties": False,
}
FORECAST_TOOL = ToolSpec(
    name=TOOL_NAME,
    description="Get today's forecast code for a city. Always use it for weather questions.",
    input_schema=FORECAST_SCHEMA,
)
CAPITAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        # maxLength is dropped on the wire by OpenAI and Anthropic and described instead.
        "country_code": {
            "type": "string",
            "minLength": 2,
            "maxLength": 2,
            "description": "Two-letter country code",
        },
    },
    "required": ["city", "country_code"],
    "additionalProperties": False,
}

# Provider bugs this suite found, by (provider, check). Each is an xfail(strict=True): the test
# still runs and, once the bug is fixed, an unexpected pass fails the suite until it is removed.
# None is open: the ones it found first (Anthropic streamed error kinds, Azure embeddings) are
# fixed, with regression tests in tests/contract.
KNOWN_BUGS: dict[tuple[str, str], str] = {}


def known_bug(request: pytest.FixtureRequest, target: Target, check: str) -> None:
    reason = KNOWN_BUGS.get((target.name, check))
    if reason:
        request.applymarker(pytest.mark.xfail(strict=True, reason=reason))


class RecordingMeter:
    """A usage meter that keeps what the router reserves, settles and releases."""

    def __init__(self) -> None:
        self.reserved: list[MeterCall] = []
        self.settled: list[tuple[TokenUsage, bool]] = []
        self.released = 0

    async def reserve(
        self, *, max_input_tokens: int, max_output_tokens: int, call: MeterCall
    ) -> object:
        self.reserved.append(call)
        return len(self.reserved)

    async def settle(self, ticket: object, usage: TokenUsage, *, estimated: bool = False) -> None:
        self.settled.append((usage, estimated))

    async def release(self, ticket: object) -> None:
        self.released += 1


@dataclass
class Live:
    target: Target
    provider: OpenAICompatibleProvider
    capabilities: ModelCapabilities
    router: ModelRouter
    meter: RecordingMeter
    client: httpx.AsyncClient
    transport: TrackingTransport
    server: LiveFakeAPI | None
    base_url: str | None
    sent: list[dict[str, Any]] = field(default_factory=list)

    def profile(self, *required: str) -> ModelProfile:
        return ModelProfile(
            name="live",
            provider=self.target.name,
            model=self.target.model,
            required_capabilities=frozenset({"chat", *required}),
            extensions=self.target.extensions,
        )

    def request(self, *messages: Message, **updates: Any) -> ModelRequest:
        values: dict[str, Any] = {
            "messages": messages,
            "model": self.target.model,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "timeout_seconds": 90,
            "extensions": self.target.extensions,
        }
        values.update(updates)
        return ModelRequest(**values)

    def variant(self, *, secrets: Any = None, model: str | None = None) -> OpenAICompatibleProvider:
        return build_provider(
            self.target,
            base_url=self.base_url,
            client=self.client,
            secrets=secrets or LiveSecrets(),
            model=model,
        )

    def require(self, capability: str) -> None:
        if not getattr(self.capabilities, capability):
            pytest.skip(f"{self.target.name} does not claim {capability}")


@pytest.fixture(params=[target.name for target in selected()])
def target(request: pytest.FixtureRequest) -> Target:
    chosen = BY_NAME[str(request.param)]
    if reason := unavailable(chosen):
        pytest.skip(reason)
    return chosen


@pytest.fixture
async def live(target: Target) -> AsyncIterator[Live]:
    async with AsyncExitStack() as stack:
        server: LiveFakeAPI | None = None
        base_url: str | None = None
        if FAKE:
            server = await stack.enter_async_context(LiveFakeAPI())
            base_url = server.base_url(target.name)
        sent: list[dict[str, Any]] = []

        async def record(request: httpx.Request) -> None:
            # Bodies only: headers carry the key and are never kept.
            try:
                sent.append(json.loads(request.content))
            except ValueError:
                pass

        transport = TrackingTransport()
        client = httpx.AsyncClient(transport=transport, event_hooks={"request": [record]})
        stack.push_async_callback(client.aclose)
        provider = build_provider(target, base_url=base_url, client=client, secrets=LiveSecrets())
        meter = RecordingMeter()
        router = ModelRouter(circuit_failure_threshold=100, usage_meter=meter)
        router.register_provider(provider, 600, COST_IN, COST_OUT, registration_id=target.name)
        yield Live(
            target=target,
            provider=provider,
            capabilities=await provider.capabilities(target.model),
            router=router,
            meter=meter,
            client=client,
            transport=transport,
            server=server,
            base_url=base_url,
            sent=sent,
        )


def user(text: str, *blocks: Any) -> Message:
    return Message(role=Role.USER, content=(TextBlock(text=text), *blocks))


async def collect(
    events: AsyncIterator[ModelStreamEvent],
) -> tuple[list[str], ModelResponse, list[Any]]:
    deltas: list[str] = []
    calls: list[Any] = []
    completed: ModelResponse | None = None
    async with aclosing(events) as stream:  # type: ignore[type-var]
        async for event in stream:
            if event.type == "delta" and event.delta:
                deltas.append(event.delta)
            elif event.type == "tool_call":
                calls.append(event.tool_call)
            elif event.type == "completed":
                completed = event.response
    assert completed is not None, "the stream ended without a completed event"
    return deltas, completed, calls


async def call(live: Live, request: ModelRequest, stream: bool, *required: str) -> ModelResponse:
    profiles = [live.profile(*required)]
    if not stream:
        return await live.router.generate(request, profiles)
    deltas, completed, calls = await collect(live.router.stream(request, profiles))
    assert "".join(deltas) == completed.message.text_content
    assert tuple(calls) == completed.tool_calls
    return completed


def assert_usage(usage: TokenUsage) -> None:
    assert usage.input_tokens > 0, usage
    assert usage.output_tokens > 0, usage
    assert usage.cached_tokens <= usage.input_tokens, usage
    expected = (usage.input_tokens * COST_IN + usage.output_tokens * COST_OUT) / 1000
    assert usage.estimated_cost_usd == pytest.approx(expected), usage


def stream_ids(values: Sequence[bool]) -> list[str]:
    return ["stream" if value else "generate" for value in values]


# -- plain generate and streaming ---------------------------------------------------------------


async def test_generate(live: Live) -> None:
    response = await live.router.generate(
        live.request(user("Reply with the single word pong and nothing else.")), [live.profile()]
    )

    assert "pong" in response.message.text_content.lower()
    assert response.finish_reason == FinishReason.STOP
    assert response.provider == live.provider.provider_id
    assert response.model
    assert_usage(response.usage)
    # The meter was charged exactly what the provider reported.
    [(settled, estimated)] = live.meter.settled
    assert not estimated
    assert (settled.input_tokens, settled.output_tokens) == (
        response.usage.input_tokens,
        response.usage.output_tokens,
    )


async def test_stream(live: Live) -> None:
    live.require("streaming")
    request = live.request(user("Reply with exactly these three words: alpha beta gamma."))

    deltas, completed, _ = await collect(live.router.stream(request, [live.profile()]))

    assert deltas, "no text deltas"
    assert "".join(deltas) == completed.message.text_content
    assert "gamma" in completed.message.text_content.lower()
    assert completed.finish_reason == FinishReason.STOP
    # Streamed calls report usage too (stream_options.include_usage, or unasked).
    assert_usage(completed.usage)
    if live.provider._stream_usage and not isinstance(live.provider, AnthropicProvider):
        assert live.sent[-1]["stream_options"]["include_usage"] is True
    [(settled, estimated)] = live.meter.settled
    assert not estimated, "the stream reported no usage, so the meter was charged an estimate"
    assert settled.input_tokens == completed.usage.input_tokens


async def test_stream_and_generate_count_the_same_input(live: Live) -> None:
    live.require("streaming")
    request = live.request(user("Reply with the single word pong and nothing else."))

    generated = await live.router.generate(request, [live.profile()])
    _, streamed, _ = await collect(live.router.stream(request, [live.profile()]))

    a, b = generated.usage.input_tokens, streamed.usage.input_tokens
    assert abs(a - b) <= max(2, a // 10), (a, b)


# -- tools ------------------------------------------------------------------------------------


@pytest.mark.parametrize("stream", [False, True], ids=stream_ids([False, True]))
async def test_tool_call_round_trip(live: Live, stream: bool) -> None:
    live.require("tools")
    if stream:
        live.require("streaming")
    messages = (
        Message.text(Role.SYSTEM, "You answer weather questions with the forecast tool."),
        user("What is today's forecast code for Paris? Use the tool."),
    )

    first = await call(live, live.request(*messages, tools=(FORECAST_TOOL,)), stream, "tools")

    assert first.tool_calls, f"no tool call: {first.message.text_content!r}"
    assert first.finish_reason == FinishReason.TOOL_CALLS
    for tool_call in first.tool_calls:
        # The wire alias (live__get_forecast) maps back to the Runtime name.
        assert tool_call.name == TOOL_NAME
        assert isinstance(tool_call.arguments, dict)
        assert "paris" in str(tool_call.arguments.get("city", "")).lower(), tool_call.arguments
    assert_usage(first.usage)

    follow = (
        *messages,
        Message(role=Role.ASSISTANT, content=first.message.content, tool_calls=first.tool_calls),
        *(
            Message(
                role=Role.TOOL,
                content=(TextBlock(text=f"Forecast code: {FORECAST_CODE}"),),
                tool_call_id=tool_call.id,
                name=TOOL_NAME,
            )
            for tool_call in first.tool_calls
        ),
    )
    final = await call(live, live.request(*follow, tools=(FORECAST_TOOL,)), stream, "tools")

    assert not final.tool_calls, final.tool_calls
    assert "4217" in final.message.text_content, final.message.text_content
    assert_usage(final.usage)


# -- structured output -------------------------------------------------------------------------


@pytest.mark.parametrize("stream", [False, True], ids=stream_ids([False, True]))
async def test_structured_output(live: Live, stream: bool) -> None:
    strict = live.capabilities.json_schema
    if not strict:
        live.require("structured_output")
    if stream:
        live.require("streaming")
    request = live.request(
        user("Give the capital city of France and France's two-letter country code."),
        response_schema=CAPITAL_SCHEMA,
    )

    response = await call(live, request, stream, "json_schema" if strict else "structured_output")

    text = response.message.text_content.strip()
    if not strict:
        # Best effort: a fenced block is accepted; the Runtime's verifier checks the rest.
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    data = json.loads(text)
    if strict:
        jsonschema.validate(data, CAPITAL_SCHEMA)
    assert str(data["city"]).lower() == "paris", data
    assert str(data["country_code"]).upper() == "FR", data


# -- images and files ---------------------------------------------------------------------------


async def test_image_input(live: Live) -> None:
    live.require("images")
    png = red_png()
    question = "What single colour fills this image? Answer with one word."
    inline = ImageBlock(data=base64.b64encode(png).decode(), media_type="image/png")
    # The attachment path: an uploaded image reaches the provider as a loaded FileBlock.
    attached = FileBlock(artifact_id="live-red").with_content(
        png, name="red.png", media_type="image/png"
    )

    for block in (inline, attached):
        response = await live.router.generate(
            live.request(user(question, block)), [live.profile("images")]
        )
        assert "red" in response.message.text_content.lower(), response.message.text_content


async def test_pdf_input(live: Live) -> None:
    live.require("files")
    pdf = FileBlock(artifact_id="live-pdf").with_content(
        code_pdf(), name="code.pdf", media_type="application/pdf"
    )

    response = await live.router.generate(
        live.request(user("What code is written in this PDF? Reply with the code only.", pdf)),
        [live.profile("files")],
    )

    assert PDF_CODE.split("-")[-1] in response.message.text_content, response.message.text_content


async def test_embeddings(live: Live, request: pytest.FixtureRequest) -> None:
    live.require("embeddings")
    model = live.target.embedding_model
    if not model:
        pytest.skip(f"set {live.target.env_prefix}_EMBED_MODEL to check embeddings")
    known_bug(request, live.target, "embeddings")

    vectors = await live.provider.embed(["hello", "world"], model=model)

    assert len(vectors) == 2
    assert len(vectors[0]) == len(vectors[1]) > 0
    assert all(isinstance(value, float) for value in vectors[0])
    assert any(value != 0 for value in vectors[0])
    assert vectors[0] != vectors[1]


# -- error mapping ------------------------------------------------------------------------------


async def failure(
    provider: OpenAICompatibleProvider, request: ModelRequest, stream: bool
) -> ProviderError:
    with pytest.raises(ProviderError) as raised:
        if stream:
            await collect(provider.stream(request))
        else:
            await provider.generate(request)
    return raised.value


@pytest.mark.parametrize("stream", [False, True], ids=stream_ids([False, True]))
async def test_wrong_key_is_a_non_retryable_authentication_error(
    live: Live, stream: bool, request: pytest.FixtureRequest
) -> None:
    if live.target.key_env is None:
        pytest.skip(f"{live.target.name} takes no key")
    known_bug(request, live.target, "wrong_key_stream" if stream else "wrong_key")
    provider = live.variant(secrets=WrongKeySecrets())

    error = await failure(provider, live.request(user("Say hi.")), stream)

    assert error.error_kind == ErrorKind.AUTHENTICATION, (error.error_kind, error.status_code)
    assert not error.retryable
    # Gemini answers a wrong key with HTTP 400 INVALID_ARGUMENT (reason API_KEY_INVALID);
    # the adapter maps it to authentication all the same.
    assert error.status_code == (400 if live.target.name == "gemini" else 401)
    assert WRONG_KEY not in str(error), "the provider error quotes the key it was sent"
    if not stream:
        # Through the router too: the kind survives, and nothing is retried.
        router = ModelRouter()
        router.register_provider(provider, registration_id=live.target.name)
        with pytest.raises(ProviderError) as raised:
            await router.generate(live.request(user("Say hi.")), [live.profile()])
        assert raised.value.error_kind == ErrorKind.AUTHENTICATION
        assert not raised.value.retryable
        assert WRONG_KEY not in str(raised.value)


@pytest.mark.parametrize("stream", [False, True], ids=stream_ids([False, True]))
async def test_unknown_model_is_a_non_retryable_invalid_request(
    live: Live, stream: bool, request: pytest.FixtureRequest
) -> None:
    known_bug(request, live.target, "unknown_model_stream" if stream else "unknown_model")
    provider = live.variant(model=UNKNOWN_MODEL)

    error = await failure(provider, live.request(user("Say hi."), model=UNKNOWN_MODEL), stream)

    assert error.error_kind == ErrorKind.INVALID_REQUEST, (error.error_kind, error.status_code)
    assert not error.retryable
    # 404: the router falls back to the next model; 400: it raises at once (docs/providers.md).
    assert error.status_code in {400, 404}, error.status_code


# -- cancellation -------------------------------------------------------------------------------


async def settle_tasks(before: set[asyncio.Task[Any]], within: float = 1.0) -> None:
    """Assert that every task started since ``before`` has ended within ``within`` seconds."""
    current = asyncio.current_task()
    for _ in range(3):
        if others := asyncio.all_tasks() - before - {current}:
            await asyncio.wait(others, timeout=within)
    left = asyncio.all_tasks() - before - {current}
    assert not left, left


@pytest.mark.parametrize("how", ["cancel", "close"])
async def test_a_cancelled_live_stream_closes_the_connection(live: Live, how: str) -> None:
    live.require("streaming")
    before = asyncio.all_tasks()
    request = live.request(user(f"{SLOW_PROMPT} 200, one number per line, nothing else."))
    events = live.router.stream(request, [live.profile()])
    first_delta = asyncio.Event()

    async def consume() -> None:
        async with aclosing(events) as stream:
            async for event in stream:
                if event.delta:
                    first_delta.set()
                    if how == "close":
                        return  # the consumer stops early; aclosing closes the stream
                # Let the test cancel now, even when the rest of the answer is already buffered.
                await asyncio.sleep(0)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(first_delta.wait(), 60)
    started = time.monotonic()
    if how == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    elapsed = time.monotonic() - started

    assert elapsed < HANG_UP_SECONDS, elapsed
    # The response was closed before the cancellation finished, not by the garbage collector.
    assert live.transport.open_responses == 0
    if live.server is not None:
        await asyncio.wait_for(live.server.last.disconnected.wait(), HANG_UP_SECONDS)
        assert not live.server.last.finished
    # Output reached the caller, so the call is charged an estimate rather than released.
    assert live.meter.settled and live.meter.settled[-1][1] is True, live.meter.settled
    await settle_tasks(before)


# -- one agent run per provider, from configuration ----------------------------------------------


@pytest.mark.parametrize("stream", [False, True], ids=stream_ids([False, True]))
async def test_agent_run_with_a_tool(target: Target, stream: bool) -> None:
    from algen_agent_runtime.config.settings import AppSettings
    from algen_agent_runtime.orchestration.container import build_container
    from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolDefinition
    from algen_agent_runtime.types.contracts import RunRequest, RunStatus

    seen: list[dict[str, Any]] = []

    async def forecast(arguments: dict[str, Any], context: Any) -> Any:
        seen.append(arguments)
        return {"forecast_code": FORECAST_CODE}

    tool = Tool(
        ToolDefinition(
            name=TOOL_NAME,
            version="1.0.0",
            description=FORECAST_TOOL.description,
            input_schema=FORECAST_SCHEMA,
            output_schema={},
            side_effect=SideEffect.READ,
        ),
        forecast,
    )
    async with AsyncExitStack() as stack:
        entry: dict[str, Any] = {"type": target.provider_type, "default_model": target.model}
        environment: dict[str, str] = {}
        if FAKE:
            server = await stack.enter_async_context(LiveFakeAPI())
            entry["base_url"] = server.base_url(target.name)
            if target.key_env:
                entry["api_key"] = "env://ALGEN_LIVE_FAKE_KEY"
                environment["ALGEN_LIVE_FAKE_KEY"] = FAKE_KEY
        elif target.provider_type == "ollama":
            entry["base_url"] = ollama_base_url()
        # Live, every other provider uses its default endpoint and env:// key.
        if target.provider_type == "azure_openai":
            entry["api_version"] = azure_api_version()
            if not FAKE:
                entry["base_url"] = os.environ["AZURE_OPENAI_ENDPOINT"]
        settings = AppSettings.model_validate(
            {
                "providers": {target.name: entry},
                "agents": [
                    {
                        "name": "live-agent",
                        "version": "1.0.0",
                        "description": "Live provider contract agent",
                        "system_instructions": (
                            "You answer weather questions. Always call the forecast tool and "
                            "quote the forecast code it returns."
                        ),
                        "default_model": {
                            "name": "live",
                            "provider": target.name,
                            "model": target.model,
                            "extensions": target.extensions,
                        },
                        "enabled_tools": [TOOL_NAME],
                        "budget": {"max_output_tokens": MAX_OUTPUT_TOKENS},
                        "max_steps": 6,
                    }
                ],
            }
        )
        meter = RecordingMeter()
        container = build_container(
            settings, environment=environment, additional_tools=(tool,), usage_meter=meter
        )
        stack.push_async_callback(container.aclose)

        result = await container.runtime.run(
            RunRequest(
                agent="live-agent",
                input="What is today's forecast code for Paris?",
                tenant_id="live",
                user_id="maintainer",
                stream=stream,
            )
        )

        assert result.status is RunStatus.COMPLETED, result.error
        assert result.output and "4217" in result.output, result.output
        assert seen and "paris" in str(seen[0].get("city", "")).lower(), seen
        summary = result.execution_summary
        assert summary.tool_calls >= 1 and summary.model_calls >= 2, summary
        assert summary.usage.input_tokens > 0 and summary.usage.output_tokens > 0, summary.usage
        # Metering saw every model call of the run, with the usage the run counted.
        assert all(not estimated for _, estimated in meter.settled), meter.settled
        assert sum(usage.input_tokens for usage, _ in meter.settled) == summary.usage.input_tokens
        assert sum(usage.output_tokens for usage, _ in meter.settled) == summary.usage.output_tokens
