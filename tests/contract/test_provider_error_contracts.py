"""Provider error and embeddings contracts the live provider suite found broken (0.2.0b5).

- Anthropic streamed HTTP errors are mapped by status, as plain calls are.
- An error a stream reports after HTTP 200 (Anthropic ``error`` events, OpenAI-style
  ``{"error": ...}`` chunks) fails the stream; a cut-off answer is never completed. Through the
  router it is raised without falling back, and a run retracts the streamed text and retries.
- Azure OpenAI embeddings go to the embeddings deployment, with ``api-version``.
- ``embed`` has a timeout and turns transport failures into ``ProviderError``.
- Plain OpenAI calls report ``cached_tokens`` as streamed ones do.
- DeepSeek gets JSON mode (``json_object``) with the schema in its instructions.
- Gemini's HTTP 400 ``API_KEY_INVALID`` is an authentication error.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.exceptions.errors import ProviderError
from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.providers.adapters import (
    AnthropicProvider,
    AzureOpenAIProvider,
    DeepSeekProvider,
    GeminiProvider,
    OpenAIProvider,
)
from algen_agent_runtime.models.providers.openai_compatible import OpenAICompatibleProvider
from algen_agent_runtime.types.contracts import (
    ErrorKind,
    Message,
    ModelProfile,
    ModelRequest,
    Role,
    RunRequest,
    RunStatus,
)

KEY = "fake-provider-key-for-tests"
# Long enough for the run's stream guard to release it as model.delta before the error.
PARTIAL = "Partial answer that the provider breaks off before it ends. " * 6
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
    "additionalProperties": False,
}


class Secrets:
    async def get(self, reference: str) -> str:
        return KEY


def request(stream: bool = False, **updates: Any) -> ModelRequest:
    return ModelRequest(
        messages=(Message.text(Role.USER, "hi"),), model="m", stream=stream, **updates
    )


def sse(*events: Any, done: bool = False) -> str:
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return body + ("data: [DONE]\n\n" if done else "")


def client(respond: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


def anthropic(respond: Callable[[httpx.Request], httpx.Response]) -> AnthropicProvider:
    return AnthropicProvider("env://K", "m", secret_provider=Secrets(), client=client(respond))


def openai(respond: Callable[[httpx.Request], httpx.Response]) -> OpenAIProvider:
    return OpenAIProvider("env://K", "m", secret_provider=Secrets(), client=client(respond))


async def drain(provider: OpenAICompatibleProvider, model_request: ModelRequest) -> list[Any]:
    return [event async for event in provider.stream(model_request)]


async def failure(provider: OpenAICompatibleProvider, stream: bool) -> ProviderError:
    with pytest.raises(ProviderError) as raised:
        if stream:
            await drain(provider, request(stream=True))
        else:
            await provider.generate(request())
    return raised.value


def anthropic_text(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "message_start", "message": {"id": "a", "usage": {"input_tokens": 3}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
    ]


def anthropic_end() -> list[dict[str, Any]]:
    return [
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 2},
        },
        {"type": "message_stop"},
    ]


def openai_text(text: str) -> dict[str, Any]:
    return {"id": "c", "choices": [{"index": 0, "delta": {"content": text}}]}


def openai_end() -> list[dict[str, Any]]:
    return [
        {"id": "c", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"id": "c", "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    ]


# -- 1. Anthropic streamed HTTP errors -------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [
        (400, ErrorKind.INVALID_REQUEST, False),
        (401, ErrorKind.AUTHENTICATION, False),
        (403, ErrorKind.AUTHORIZATION, False),
        (404, ErrorKind.INVALID_REQUEST, False),
        (422, ErrorKind.INVALID_REQUEST, False),
        (429, ErrorKind.RATE_LIMIT, True),
        (500, ErrorKind.UNAVAILABLE, True),
        (529, ErrorKind.UNAVAILABLE, True),
    ],
)
@pytest.mark.parametrize("stream", [False, True], ids=["generate", "stream"])
async def test_anthropic_http_errors_map_by_status_streamed_or_not(
    status: int, kind: ErrorKind, retryable: bool, stream: bool
) -> None:
    body = {"type": "error", "error": {"type": "x_error", "message": f"bad key {KEY}"}}
    provider = anthropic(lambda _: httpx.Response(status, json=body))

    error = await failure(provider, stream)

    assert (error.error_kind, error.retryable, error.status_code) == (kind, retryable, status)
    # The error text is kept, masked.
    assert "bad key" in str(error) and "x_error" in str(error)
    assert KEY not in str(error)


# -- 2. errors reported inside a stream --------------------------------------------------------


@pytest.mark.parametrize(
    ("error_type", "kind", "retryable"),
    [
        ("overloaded_error", ErrorKind.UNAVAILABLE, True),
        ("api_error", ErrorKind.UNAVAILABLE, True),
        ("rate_limit_error", ErrorKind.RATE_LIMIT, True),
        ("invalid_request_error", ErrorKind.INVALID_REQUEST, False),
        ("authentication_error", ErrorKind.AUTHENTICATION, False),
        ("something_new_error", ErrorKind.UNAVAILABLE, True),
    ],
)
async def test_an_anthropic_error_event_fails_the_stream(
    error_type: str, kind: ErrorKind, retryable: bool
) -> None:
    body = sse(
        *anthropic_text("Hal"),
        {"type": "error", "error": {"type": error_type, "message": f"Overloaded {KEY}"}},
    )
    provider = anthropic(lambda _: httpx.Response(200, text=body))
    seen: list[Any] = []

    with pytest.raises(ProviderError) as raised:
        async for event in provider.stream(request(stream=True)):
            seen.append(event)

    assert [event.delta for event in seen] == ["Hal"]  # no completed event
    error = raised.value
    assert (error.error_kind, error.retryable, error.status_code) == (kind, retryable, None)
    assert "Overloaded" in str(error) and error_type in str(error)
    assert KEY not in str(error)


@pytest.mark.parametrize(
    ("error", "kind", "retryable"),
    [
        (
            {"message": "The server had an error", "type": "server_error"},
            ErrorKind.UNAVAILABLE,
            True,
        ),
        ({"message": "Slow down", "type": "rate_limit_exceeded"}, ErrorKind.RATE_LIMIT, True),
        ({"message": "Overloaded", "code": 529}, ErrorKind.UNAVAILABLE, True),
        ({"message": "Too many", "code": "429"}, ErrorKind.RATE_LIMIT, True),
        (
            {"message": "context too long", "type": "invalid_request_error"},
            ErrorKind.INVALID_REQUEST,
            False,
        ),
        (
            {"message": "Boom", "code": 400, "status": "INVALID_ARGUMENT"},
            ErrorKind.INVALID_REQUEST,
            False,
        ),
        ({"message": "mystery"}, ErrorKind.UNAVAILABLE, True),
    ],
)
async def test_an_openai_style_error_chunk_fails_the_stream(
    error: dict[str, Any], kind: ErrorKind, retryable: bool
) -> None:
    leaky = {**error, "message": f"{error['message']} (key {KEY})"}
    body = sse(openai_text("Hal"), {"error": leaky}, done=True)
    provider = openai(lambda _: httpx.Response(200, text=body))
    seen: list[Any] = []

    with pytest.raises(ProviderError) as raised:
        async for event in provider.stream(request(stream=True)):
            seen.append(event)

    assert [event.delta for event in seen] == ["Hal"]
    raised_error = raised.value
    assert (raised_error.error_kind, raised_error.retryable) == (kind, retryable)
    assert raised_error.status_code is None
    assert error["message"] in str(raised_error)
    assert KEY not in str(raised_error)


class Recorder:
    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        self.calls = 0

    def respond(self, body: str) -> Callable[[httpx.Request], httpx.Response]:
        def handler(_: httpx.Request) -> httpx.Response:
            self.calls += 1
            return httpx.Response(200, text=body)

        return handler


@pytest.mark.parametrize("vendor", ["anthropic", "openai"])
async def test_the_router_raises_a_mid_stream_error_without_falling_back(vendor: str) -> None:
    if vendor == "anthropic":
        broken: OpenAICompatibleProvider = anthropic(
            lambda _: httpx.Response(
                200,
                text=sse(
                    *anthropic_text("Hal"),
                    {"type": "error", "error": {"type": "overloaded_error", "message": "x"}},
                ),
            )
        )
    else:
        broken = openai(
            lambda _: httpx.Response(
                200, text=sse(openai_text("Hal"), {"error": {"type": "server_error"}})
            )
        )
    backup = Recorder("backup")
    fallback = OpenAICompatibleProvider(
        "backup",
        "https://backup.example/v1",
        None,
        "b",
        broken._capabilities,
        client=client(backup.respond(sse(openai_text("Hello"), *openai_end(), done=True))),
    )
    router = ModelRouter()
    router.register_provider(broken, registration_id="primary")
    router.register_provider(fallback, registration_id="backup")
    profiles = [
        ModelProfile(name="p", provider="primary", model="m"),
        ModelProfile(name="b", provider="backup", model="b"),
    ]
    deltas: list[str] = []

    with pytest.raises(ProviderError) as raised:
        async for event in router.stream(request(stream=True), profiles):
            if event.delta:
                deltas.append(event.delta)

    # The text already delivered is not followed by another model's answer.
    assert deltas == ["Hal"]
    assert backup.calls == 0
    assert raised.value.retryable


def _runtime_for(provider: OpenAICompatibleProvider) -> Any:
    agent = make_agent(
        default_model=ModelProfile(name="live", provider=provider.provider_id, model="m")
    )
    runtime = make_runtime(agent=agent)
    # A router whose circuit stays closed after one failure, so the retry reaches the provider.
    runtime.router = ModelRouter()
    runtime.router.register_provider(provider)
    return runtime


def _stream_once_broken(vendor: str, *, retryable: bool = True) -> OpenAICompatibleProvider:
    calls = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if vendor == "anthropic":
            error_type = "overloaded_error" if retryable else "invalid_request_error"
            if calls == 1:
                events = [
                    *anthropic_text(PARTIAL),
                    {"type": "error", "error": {"type": error_type, "message": "Overloaded"}},
                ]
            else:
                events = [*anthropic_text("The full answer."), *anthropic_end()]
            return httpx.Response(200, text=sse(*events))
        error = {"type": "server_error" if retryable else "invalid_request_error"}
        if calls == 1:
            return httpx.Response(200, text=sse(openai_text(PARTIAL), {"error": error}))
        return httpx.Response(
            200, text=sse(openai_text("The full answer."), *openai_end(), done=True)
        )

    return anthropic(respond) if vendor == "anthropic" else openai(respond)


@pytest.mark.parametrize("vendor", ["anthropic", "openai"])
async def test_a_streamed_run_retracts_a_broken_off_answer_and_retries(vendor: str) -> None:
    runtime = _runtime_for(_stream_once_broken(vendor))

    result = await runtime.run(
        RunRequest(agent="test-agent", input="hi", tenant_id="t", user_id="u", stream=True)
    )

    assert result.status == RunStatus.COMPLETED, result.error
    assert result.output == "The full answer."
    events = list(await runtime.events.history(result.run_id))
    types = [event.type for event in events]
    [retracted] = [event for event in events if event.type == "model.delta.retracted"]
    assert retracted.data["reason"] == "model_error"
    assert "model.retrying" in types
    after = events[events.index(retracted) + 1 :]
    # Clients that drop the retracted text see the answer once, not the partial text with it.
    assert "".join(e.data["delta"] for e in after if e.type == "model.delta") == result.output
    assert result.execution_summary.model_calls >= 1


@pytest.mark.parametrize("vendor", ["anthropic", "openai"])
async def test_a_non_retryable_mid_stream_error_fails_the_run_without_a_partial_answer(
    vendor: str,
) -> None:
    runtime = _runtime_for(_stream_once_broken(vendor, retryable=False))

    result = await runtime.run(
        RunRequest(agent="test-agent", input="hi", tenant_id="t", user_id="u", stream=True)
    )

    assert result.status == RunStatus.FAILED
    assert not result.output or "Partial answer" not in result.output
    events = list(await runtime.events.history(result.run_id))
    assert [e.data["reason"] for e in events if e.type == "model.delta.retracted"] == [
        "model_error"
    ]


# -- 3. Azure OpenAI embeddings -----------------------------------------------------------------


def azure(respond: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> AzureOpenAIProvider:
    return AzureOpenAIProvider(
        "https://res.openai.azure.com/",
        "env://K",
        "chat-deployment",
        "2024-10-21",
        Secrets(),
        client(respond),
        **kwargs,
    )


def embeddings_ok(seen: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def respond(sent: httpx.Request) -> httpx.Response:
        seen.append(sent)
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})

    return respond


@pytest.mark.parametrize(
    ("model", "configured", "deployment"),
    [("embed-small", None, "embed-small"), (None, "embed-cfg", "embed-cfg"), ("x", "y", "x")],
)
async def test_azure_embeddings_post_to_the_embeddings_deployment(
    model: str | None, configured: str | None, deployment: str
) -> None:
    seen: list[httpx.Request] = []
    provider = azure(embeddings_ok(seen), embedding_deployment=configured)

    vectors = await provider.embed(["hello"], model=model)

    assert vectors == [[0.1, 0.2]]
    [sent] = seen
    assert sent.url.path == f"/openai/deployments/{deployment}/embeddings"
    assert sent.url.params["api-version"] == "2024-10-21"
    assert sent.headers["api-key"] == KEY
    assert "authorization" not in sent.headers
    assert json.loads(sent.content) == {"input": ["hello"]}


async def test_azure_embeddings_without_a_deployment_fail_before_any_request() -> None:
    seen: list[httpx.Request] = []
    provider = azure(embeddings_ok(seen))

    with pytest.raises(ProviderError) as raised:
        await provider.embed(["hello"])

    assert raised.value.error_kind == ErrorKind.INVALID_REQUEST
    assert not raised.value.retryable
    assert "embedding_deployment" in str(raised.value)
    assert not seen


def test_build_container_passes_the_azure_embedding_deployment() -> None:
    from algen_agent_runtime.config.settings import AppSettings
    from algen_agent_runtime.orchestration.container import build_container

    settings = AppSettings.model_validate(
        {
            "providers": {
                "azure": {
                    "type": "azure_openai",
                    "base_url": "https://res.openai.azure.com",
                    "api_key": "env://AZURE_OPENAI_API_KEY",
                    "default_model": "chat",
                    "embedding_deployment": "embed-small",
                }
            }
        }
    )
    container = build_container(settings, environment={"AZURE_OPENAI_API_KEY": "k"})
    [provider] = [
        item for item in container.runtime.router.providers() if item.provider_id == "azure_openai"
    ]

    assert isinstance(provider, AzureOpenAIProvider)
    assert provider._embeddings_url(None) == (
        "https://res.openai.azure.com/openai/deployments/embed-small/embeddings"
        "?api-version=2024-10-21"
    )


# -- 4. embed timeouts and transport errors; cached tokens -----------------------------------------


@pytest.mark.parametrize(
    ("failure_type", "kind"),
    [
        (httpx.ReadTimeout, ErrorKind.TIMEOUT),
        (httpx.ConnectTimeout, ErrorKind.TIMEOUT),
        (httpx.ConnectError, ErrorKind.UNAVAILABLE),
        (httpx.RemoteProtocolError, ErrorKind.UNAVAILABLE),
    ],
)
async def test_embed_turns_transport_failures_into_retryable_provider_errors(
    failure_type: type[httpx.TransportError], kind: ErrorKind
) -> None:
    def respond(sent: httpx.Request) -> httpx.Response:
        raise failure_type("boom", request=sent)

    with pytest.raises(ProviderError) as raised:
        await openai(respond).embed(["hello"])

    assert (raised.value.error_kind, raised.value.retryable) == (kind, True)


async def test_embed_has_a_timeout() -> None:
    seen: list[httpx.Request] = []
    provider = openai(embeddings_ok(seen))
    provider.embed_timeout_seconds = 7.5

    await provider.embed(["hello"], model="text-embedding-3-small")

    assert seen[0].extensions["timeout"] == {
        "connect": 7.5,
        "read": 7.5,
        "write": 7.5,
        "pool": 7.5,
    }


async def test_embed_reports_an_invalid_response_as_a_provider_error() -> None:
    provider = openai(lambda _: httpx.Response(200, json={"object": "list"}))

    with pytest.raises(ProviderError) as raised:
        await provider.embed(["hello"])

    assert raised.value.error_kind == ErrorKind.INVALID_RESPONSE


async def test_plain_and_streamed_calls_report_cached_tokens_alike() -> None:
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 5,
        "prompt_tokens_details": {"cached_tokens": 64},
    }

    def respond(sent: httpx.Request) -> httpx.Response:
        if json.loads(sent.content)["stream"]:
            events = [
                openai_text("pong"),
                *openai_end()[:1],
                {"id": "c", "choices": [], "usage": usage},
            ]
            return httpx.Response(200, text=sse(*events, done=True))
        message = {"role": "assistant", "content": "pong"}
        return httpx.Response(
            200,
            json={
                "id": "c",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": usage,
            },
        )

    provider = openai(respond)
    plain = await provider.generate(request())
    streamed = (await drain(provider, request(stream=True)))[-1].response

    assert plain.usage.cached_tokens == streamed.usage.cached_tokens == 64
    assert plain.usage.input_tokens == streamed.usage.input_tokens == 100


# -- 5. DeepSeek JSON mode ------------------------------------------------------------------------


async def test_deepseek_sends_json_mode_with_the_schema_in_its_instructions() -> None:
    sent: list[dict[str, Any]] = []

    def respond(item: httpx.Request) -> httpx.Response:
        sent.append(json.loads(item.content))
        message = {"role": "assistant", "content": '{"city": "Paris"}'}
        return httpx.Response(
            200,
            json={
                "id": "d",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            },
        )

    provider = DeepSeekProvider("env://K", secret_provider=Secrets(), client=client(respond))
    capabilities = await provider.capabilities("deepseek-chat")
    model_request = ModelRequest(
        messages=(
            Message.text(Role.SYSTEM, "Be brief."),
            Message.text(Role.SYSTEM, "Evidence."),
            Message.text(Role.USER, "Capital of France?"),
        ),
        model="deepseek-chat",
        response_schema=SCHEMA,
    )

    response = await provider.generate(model_request)

    assert capabilities.structured_output and not capabilities.json_schema
    [body] = sent
    assert body["response_format"] == {"type": "json_object"}
    roles = [message["role"] for message in body["messages"]]
    assert roles == ["system", "system", "system", "user"]
    instruction = body["messages"][2]["content"]
    # DeepSeek's JSON mode needs the word "json" in the prompt; the schema is there in full.
    assert "JSON" in instruction
    assert json.loads(instruction.split("JSON Schema: ", 1)[1]) == SCHEMA
    assert response.message.text_content == '{"city": "Paris"}'


async def test_deepseek_without_a_schema_sends_no_response_format() -> None:
    sent: list[dict[str, Any]] = []

    def respond(item: httpx.Request) -> httpx.Response:
        sent.append(json.loads(item.content))
        message = {"role": "assistant", "content": "hi"}
        return httpx.Response(
            200,
            json={
                "id": "d",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            },
        )

    provider = DeepSeekProvider("env://K", secret_provider=Secrets(), client=client(respond))
    await provider.generate(request())

    assert "response_format" not in sent[0]
    assert [message["role"] for message in sent[0]["messages"]] == ["user"]


# -- 6. Gemini's wrong-key answer ---------------------------------------------------------------

GEMINI_KEY_ERROR = [
    {
        "error": {
            "code": 400,
            "message": "API key not valid. Please pass a valid API key.",
            "status": "INVALID_ARGUMENT",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "API_KEY_INVALID",
                    "domain": "googleapis.com",
                }
            ],
        }
    }
]


@pytest.mark.parametrize("body", [GEMINI_KEY_ERROR, GEMINI_KEY_ERROR[0]], ids=["list", "object"])
@pytest.mark.parametrize("stream", [False, True], ids=["generate", "stream"])
async def test_gemini_api_key_invalid_is_an_authentication_error(body: Any, stream: bool) -> None:
    provider = GeminiProvider(
        "env://K",
        secret_provider=Secrets(),
        client=client(lambda _: httpx.Response(400, json=body)),
    )

    error = await failure(provider, stream)

    assert (error.error_kind, error.retryable, error.status_code) == (
        ErrorKind.AUTHENTICATION,
        False,
        400,
    )
    assert "API key not valid" in str(error) and "INVALID_ARGUMENT" in str(error)


async def test_another_gemini_400_stays_an_invalid_request() -> None:
    body = [{"error": {"code": 400, "message": "Bad schema", "status": "INVALID_ARGUMENT"}}]
    provider = GeminiProvider(
        "env://K",
        secret_provider=Secrets(),
        client=client(lambda _: httpx.Response(400, json=body)),
    )

    error = await failure(provider, stream=False)

    assert error.error_kind == ErrorKind.INVALID_REQUEST
    assert "Bad schema" in str(error)
