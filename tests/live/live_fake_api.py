"""A local stand-in for the vendor APIs, so the live contract suite can prove its own logic offline.

``ALGEN_LIVE_FAKE=1`` points every provider of ``tests/live`` at this server instead of the real
service. It builds on ``tests/fake_model_server.py`` (the same HTTP plumbing and hang-up
tracking the cancellation tests use) and answers the way the real APIs do, closely enough that a
provider class that sends the wrong shape, or reads the answer wrongly, fails here too:

- every provider is served under its own path prefix (``/openai/chat/completions``,
  ``/azure/openai/deployments/<d>/chat/completions``, ``/anthropic/messages``, ...), so the
  server knows which vendor's dialect to speak (Mistral's ``image_url`` string and unasked
  stream usage, Anthropic's Messages events);
- a request without the expected key gets HTTP 401 (with the key quoted back, as OpenAI does,
  so the suite can check it is masked), except Gemini, which answers HTTP 400 ``API_KEY_INVALID``
  as Google does; an unknown model or deployment gets HTTP 404;
- DeepSeek rejects ``json_schema`` response formats (it takes ``json_object`` only) and answers a
  ``json_object`` request with an instance of the schema its instructions carry;
- a request with tools gets a tool call (arguments filled from the tool's schema), a request
  that carries a tool result gets an answer that quotes it, a ``json_schema`` request gets a JSON
  instance of the schema, an image gets "Red.", a PDF gets the ``ALGEN-PDF-<n>`` code read
  from its bytes, and "Count from 1 to" streams numbers slowly until the client hangs up;
- streamed usage is sent only when asked for (``stream_options.include_usage``), except for
  Mistral and Anthropic, which report it unasked.

It is test code: one request per connection, no TLS, 127.0.0.1 only.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from typing import Any

from fake_model_server import Exchange, FakeModelServer

FAKE_KEY = "fake-live-key-for-tests"
UNKNOWN_MODEL = "algen-live-no-such-model"
SLOW_PROMPT = "Count from 1 to"
_PDF_CODE = re.compile(rb"ALGEN-PDF-\d+")
# What precedes the schema a JSON-mode request (DeepSeek) carries in its instructions.
_SCHEMA_MARKER = "JSON Schema: "


class _Rejected(Exception):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(status)
        self.status = status
        self.body = body


def _openai_error(status: int, message: str, code: str) -> _Rejected:
    return _Rejected(
        status, {"error": {"message": message, "type": "invalid_request_error", "code": code}}
    )


def _gemini_key_error() -> _Rejected:
    # Google answers a wrong key with HTTP 400 INVALID_ARGUMENT, reason API_KEY_INVALID, and the
    # OpenAI-compatible endpoint wraps the error in a one-item list.
    return _Rejected(
        400,
        [
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
                            "metadata": {"service": "generativelanguage.googleapis.com"},
                        }
                    ],
                }
            }
        ],
    )


def _anthropic_error(status: int, kind: str, message: str) -> _Rejected:
    return _Rejected(status, {"type": "error", "error": {"type": kind, "message": message}})


def _instance(schema: dict[str, Any]) -> Any:
    """A small value that satisfies ``schema`` (the subset the suite sends)."""
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        return _instance(schema["anyOf"][0])
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "null")
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties") or {}
        return {name: _instance(value) for name, value in properties.items()}
    if kind == "array":
        return [_instance(schema.get("items") or {})]
    if kind == "integer":
        return 2
    if kind == "number":
        return 2.5
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    return "FR" if "country" in json.dumps(schema).lower() else "Paris"


def _tokens(value: Any) -> int:
    return max(1, len(json.dumps(value)) // 4)


class _Reply:
    def __init__(
        self, text: str = "", call: tuple[str, dict[str, Any]] | None = None, slow: bool = False
    ) -> None:
        self.text = text
        self.call = call
        self.slow = slow


class LiveFakeAPI(FakeModelServer):
    """Speaks each vendor's dialect under ``/<vendor>/...``; see the module docstring."""

    def __init__(self) -> None:
        super().__init__(finish=True, delay=0)

    async def __aenter__(self) -> LiveFakeAPI:
        await super().__aenter__()
        return self

    def base_url(self, vendor: str) -> str:
        return f"{self.url}/{vendor}"

    # -- request checks ---------------------------------------------------------------------

    @staticmethod
    def _vendor(exchange: Exchange) -> str:
        return exchange.path.lstrip("/").split("/", 1)[0]

    def _check(self, exchange: Exchange) -> None:
        vendor = self._vendor(exchange)
        headers = exchange.headers
        if vendor == "anthropic":
            if headers.get("x-api-key") != FAKE_KEY:
                raise _anthropic_error(401, "authentication_error", "invalid x-api-key")
            if not headers.get("anthropic-version"):
                raise _anthropic_error(400, "invalid_request_error", "anthropic-version missing")
            if str(exchange.body.get("model", "")).startswith(UNKNOWN_MODEL):
                raise _anthropic_error(404, "not_found_error", "model: not found")
            return
        if vendor == "azure":
            sent = headers.get("api-key", "")
            if sent != FAKE_KEY:
                raise _openai_error(401, f"Access denied: invalid key {sent}", "401")
            if f"/deployments/{UNKNOWN_MODEL}/" in exchange.path:
                raise _openai_error(404, "The API deployment does not exist", "DeploymentNotFound")
            if "api-version=" not in exchange.path:
                raise _openai_error(404, "Resource not found", "404")
            return
        if vendor != "ollama":
            sent = headers.get("authorization", "")
            if sent != f"Bearer {FAKE_KEY}":
                if vendor == "gemini":
                    raise _gemini_key_error()
                token = sent.removeprefix("Bearer ")
                raise _openai_error(401, f"Incorrect API key provided: {token}.", "invalid_api_key")
        if str(exchange.body.get("model", "")).startswith(UNKNOWN_MODEL):
            raise _openai_error(404, "The model does not exist", "model_not_found")

    # -- what the "model" answers -----------------------------------------------------------

    def _openai_reply(self, vendor: str, body: dict[str, Any]) -> _Reply:
        messages = body.get("messages") or []
        if not messages:
            raise _openai_error(400, "messages is empty", "invalid_value")
        images = files = False
        pdf_code: str | None = None
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                kind = part.get("type")
                if kind == "image_url":
                    url = part.get("image_url")
                    # Mistral takes the data URL itself, everyone else an object around it.
                    url = url if vendor == "mistral" else (url or {}).get("url")
                    if not isinstance(url, str) or not url.startswith("data:image/"):
                        raise _openai_error(400, "invalid image_url part", "invalid_value")
                    images = True
                elif kind == "file":
                    data = (part.get("file") or {}).get("file_data", "")
                    if not data.startswith("data:application/pdf;base64,"):
                        raise _openai_error(400, "invalid file part", "invalid_value")
                    files = True
                    raw = base64.b64decode(data.split(",", 1)[1])
                    found = _PDF_CODE.search(raw)
                    pdf_code = found.group().decode() if found else None
                elif kind != "text":
                    raise _openai_error(400, f"unknown content part {kind}", "invalid_value")
        last_user = max(
            (index for index, item in enumerate(messages) if item.get("role") == "user"),
            default=-1,
        )
        results = [
            str(item.get("content") or "")
            for item in messages[last_user + 1 :]
            if item.get("role") == "tool"
        ]
        if results:
            return _Reply(f"The tool returned {results[-1]}.")
        tools = body.get("tools") or []
        if tools:
            function = tools[0]["function"]
            return _Reply(call=(function["name"], _instance(function.get("parameters") or {})))
        response_format = body.get("response_format") or {}
        if vendor == "deepseek" and response_format.get("type") not in {
            None,
            "text",
            "json_object",
        }:
            # DeepSeek takes text or json_object only.
            raise _openai_error(
                400, "This response_format type is unavailable now", "invalid_request_error"
            )
        if response_format.get("type") == "json_schema":
            schema = response_format["json_schema"]["schema"]
            return _Reply(json.dumps(_instance(schema)))
        if response_format.get("type") == "json_object":
            prompt_text = " ".join(_text(item.get("content")) for item in messages)
            if "json" not in prompt_text.lower():
                # As DeepSeek: JSON mode needs the word "json" in the prompt.
                raise _openai_error(
                    400, "Prompt must contain the word 'json'", "invalid_request_error"
                )
            at = prompt_text.find(_SCHEMA_MARKER)
            schema: dict[str, Any] = {}
            if at >= 0:
                schema, _ = json.JSONDecoder().raw_decode(prompt_text, at + len(_SCHEMA_MARKER))
            return _Reply(json.dumps(_instance(schema) if schema else {}))
        if images:
            return _Reply("Red.")
        if files:
            return _Reply(f"The code is {pdf_code}." if pdf_code else "I cannot read it.")
        prompt = _text(messages[last_user].get("content")) if last_user >= 0 else ""
        if SLOW_PROMPT in prompt:
            return _Reply(slow=True)
        return _canned(prompt)

    def _anthropic_reply(self, body: dict[str, Any]) -> _Reply:
        messages = body.get("messages") or []
        if not messages or messages[-1].get("role") != "user":
            raise _anthropic_error(400, "invalid_request_error", "must end with a user turn")
        last = messages[-1]["content"]
        blocks = last if isinstance(last, list) else [{"type": "text", "text": last}]
        results = [block for block in blocks if block.get("type") == "tool_result"]
        if results:
            content = results[-1].get("content")
            return _Reply(f"The tool returned {_text(content)}.")
        tools = body.get("tools") or []
        if tools:
            return _Reply(call=(tools[0]["name"], _instance(tools[0].get("input_schema") or {})))
        output = (body.get("output_config") or {}).get("format") or {}
        if output.get("type") == "json_schema":
            return _Reply(json.dumps(_instance(output["schema"])))
        for block in blocks:
            source = block.get("source") or {}
            if block.get("type") in {"image", "document"} and source.get("type") != "base64":
                raise _anthropic_error(400, "invalid_request_error", "bad source")
            if block.get("type") == "image":
                return _Reply("Red.")
            if block.get("type") == "document":
                found = _PDF_CODE.search(base64.b64decode(source.get("data", "")))
                return _Reply(f"The code is {found.group().decode()}." if found else "Unreadable.")
        prompt = _text(blocks)
        if SLOW_PROMPT in prompt:
            return _Reply(slow=True)
        return _canned(prompt)

    # -- FakeModelServer hooks --------------------------------------------------------------

    async def _answer(self, writer: asyncio.StreamWriter, exchange: Exchange) -> None:
        vendor = self._vendor(exchange)
        try:
            self._check(exchange)
            if exchange.path.split("?", 1)[0].endswith("/embeddings"):
                payload: dict[str, Any] = {"data": _embeddings(exchange.body.get("input") or [])}
            elif vendor == "anthropic":
                payload = self._anthropic_message(
                    exchange.body, self._anthropic_reply(exchange.body)
                )
            else:
                reply = self._openai_reply(vendor, exchange.body)
                payload = self._openai_completion(exchange.body, reply)
        except _Rejected as rejected:
            await self._send_json(writer, rejected.status, rejected.body)
            return
        await self._send_json(writer, 200, payload)
        exchange.finished = True

    async def _stream(self, writer: asyncio.StreamWriter, exchange: Exchange) -> None:
        vendor = self._vendor(exchange)
        try:
            self._check(exchange)
            reply = (
                self._anthropic_reply(exchange.body)
                if vendor == "anthropic"
                else self._openai_reply(vendor, exchange.body)
            )
        except _Rejected as rejected:
            await self._send_json(writer, rejected.status, rejected.body)
            exchange.first_chunk_sent.set()
            return
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        )
        events = (
            self._anthropic_events(exchange.body, reply)
            if vendor == "anthropic"
            else self._openai_events(vendor, exchange.body, reply)
        )
        if reply.slow:
            # One number every 20 ms, until the client hangs up or 400 have gone out.
            head = 2 if vendor == "anthropic" else 1
            for event in events[:head]:
                self._chunk(writer, _sse(event))
            for number in range(1, 401):
                self._chunk(writer, _sse(self._slow_delta(vendor, f"{number}\n")))
                await writer.drain()
                exchange.first_chunk_sent.set()
                if exchange.disconnected.is_set():
                    return
                await asyncio.sleep(0.02)
            events = events[head:]
        for event in events:
            self._chunk(writer, _sse(event))
        if vendor != "anthropic":
            self._chunk(writer, b"data: [DONE]\n\n")
        writer.write(b"0\r\n\r\n")
        await writer.drain()
        exchange.first_chunk_sent.set()
        exchange.finished = True

    # -- response bodies --------------------------------------------------------------------

    @staticmethod
    async def _send_json(writer: asyncio.StreamWriter, status: int, payload: Any) -> None:
        reasons = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found"}
        body = json.dumps(payload).encode()
        writer.write(
            f"HTTP/1.1 {status} {reasons.get(status, 'Error')}\r\n".encode()
            + b"Content-Type: application/json\r\nConnection: close\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        await writer.drain()

    @staticmethod
    def _openai_completion(body: dict[str, Any], reply: _Reply) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": reply.text or None}
        if reply.call is not None:
            name, arguments = reply.call
            message["tool_calls"] = [
                {
                    "id": "call_fake_1",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ]
        return {
            "id": "chatcmpl-fake",
            "model": body.get("model", "fake-model"),
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if reply.call else "stop",
                }
            ],
            "usage": {
                "prompt_tokens": _tokens(body.get("messages")),
                "completion_tokens": _tokens(reply.text or reply.call),
            },
        }

    @staticmethod
    def _openai_events(vendor: str, body: dict[str, Any], reply: _Reply) -> list[dict[str, Any]]:
        model = body.get("model", "fake-model")

        def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
            return {
                "id": "chatcmpl-fake",
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        events = [chunk({"role": "assistant", "content": ""})]
        if reply.call is not None:
            name, arguments = reply.call
            encoded = json.dumps(arguments)
            half = len(encoded) // 2
            events.append(
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_fake_1",
                                "type": "function",
                                "function": {"name": name, "arguments": encoded[:half]},
                            }
                        ]
                    }
                )
            )
            events.append(
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": encoded[half:]}}]})
            )
        words = re.findall(r"\S+\s*", reply.text)
        events += [chunk({"content": word}) for word in words]
        usage = {
            "prompt_tokens": _tokens(body.get("messages")),
            "completion_tokens": _tokens(reply.text or reply.call or "x" * 400),
        }
        final = chunk({}, "tool_calls" if reply.call else "stop")
        if vendor == "mistral":
            final["usage"] = usage
        events.append(final)
        if (body.get("stream_options") or {}).get("include_usage"):
            events.append({"id": "chatcmpl-fake", "model": model, "choices": [], "usage": usage})
        return events

    @staticmethod
    def _slow_delta(vendor: str, text: str) -> dict[str, Any]:
        if vendor == "anthropic":
            return {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            }
        return {"id": "chatcmpl-fake", "choices": [{"index": 0, "delta": {"content": text}}]}

    @staticmethod
    def _anthropic_message(body: dict[str, Any], reply: _Reply) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        if reply.text:
            content.append({"type": "text", "text": reply.text})
        if reply.call is not None:
            content.append(
                {
                    "type": "tool_use",
                    "id": "toolu_fake_1",
                    "name": reply.call[0],
                    "input": reply.call[1],
                }
            )
        prompt = _tokens(body.get("messages")) + _tokens(body.get("system") or "")
        return {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "fake-model"),
            "content": content,
            "stop_reason": "tool_use" if reply.call else "end_turn",
            "usage": {
                "input_tokens": prompt,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": _tokens(reply.text or reply.call),
            },
        }

    @staticmethod
    def _anthropic_events(body: dict[str, Any], reply: _Reply) -> list[dict[str, Any]]:
        prompt = _tokens(body.get("messages")) + _tokens(body.get("system") or "")
        events: list[dict[str, Any]] = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_fake",
                    "model": body.get("model", "fake-model"),
                    "usage": {"input_tokens": prompt, "output_tokens": 1},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ]
        for word in re.findall(r"\S+\s*", reply.text):
            events.append(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": word},
                }
            )
        events.append({"type": "content_block_stop", "index": 0})
        if reply.call is not None:
            name, arguments = reply.call
            encoded = json.dumps(arguments)
            half = len(encoded) // 2
            events.append(
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_fake_1",
                        "name": name,
                        "input": {},
                    },
                }
            )
            for piece in (encoded[:half], encoded[half:]):
                events.append(
                    {
                        "type": "content_block_delta",
                        "index": 1,
                        "delta": {"type": "input_json_delta", "partial_json": piece},
                    }
                )
            events.append({"type": "content_block_stop", "index": 1})
        events.append(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use" if reply.call else "end_turn"},
                "usage": {"output_tokens": _tokens(reply.text or reply.call or "x" * 400)},
            }
        )
        events.append({"type": "message_stop"})
        return events


def _canned(prompt: str) -> _Reply:
    """A plain answer: the words asked for after "these ... words:", "pong", or a greeting."""
    asked = re.search(r"these \w+ words: (.+?)\.?$", prompt)
    if asked:
        return _Reply(asked.group(1))
    return _Reply("pong" if "pong" in prompt.lower() else "Hello from the fake API.")


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            _text(block.get("text") if block.get("type") == "text" else block.get("content"))
            for block in content
            if isinstance(block, dict)
        )
    return ""


def _sse(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


def _embeddings(texts: list[str]) -> list[dict[str, Any]]:
    data = []
    for text in texts:
        digest = hashlib.sha256(str(text).encode()).digest()
        data.append({"embedding": [byte / 255 for byte in digest[:8]]})
    return data
