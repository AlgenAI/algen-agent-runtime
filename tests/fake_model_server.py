"""A local model API that streams slowly and records when the client hangs up.

Provider cancellation tests point the built-in HTTP providers at it (never at a real API, never
with a real key). It speaks just enough HTTP/1.1 for ``httpx``: it reads one request, then

- for a streamed call, sends response headers and ``chunks`` server-sent events at once, and
  then either finishes the stream (``finish=True``) or stalls until the client disconnects;
- for a plain call, answers after ``delay`` seconds (or stalls when ``delay`` is None).

Each request is an :class:`Exchange`, whose ``disconnected`` event is set the moment the
client closes the connection, so a test can tell an aborted request from one left running.
``TrackingTransport`` is the client half: it counts the responses still open, so a test can
check that a cancelled call closed its response before the cancellation finished, rather than
whenever the garbage collector gets to it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class Exchange:
    path: str
    body: dict[str, Any]
    headers: dict[str, str]
    first_chunk_sent: asyncio.Event = field(default_factory=asyncio.Event)
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)
    finished: bool = False
    disconnected_at: float | None = None

    def hang_up(self) -> None:
        if not self.disconnected.is_set():
            self.disconnected_at = time.monotonic()
            self.disconnected.set()


def _openai_chunk(text: str) -> dict[str, Any]:
    return {"id": "chatcmpl-fake", "model": "fake-model", "choices": [{"delta": {"content": text}}]}


def _openai_events(chunks: int, finish: bool) -> list[dict[str, Any]]:
    events = [_openai_chunk(f"token{index} ") for index in range(chunks)]
    if finish:
        events.append({"id": "chatcmpl-fake", "choices": [{"delta": {}, "finish_reason": "stop"}]})
        events.append(
            {
                "id": "chatcmpl-fake",
                "choices": [],
                "usage": {"prompt_tokens": 11, "completion_tokens": chunks},
            }
        )
    return events


def _anthropic_events(chunks: int, finish: bool) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {"id": "msg-fake", "model": "fake-model", "usage": {"input_tokens": 11}},
        }
    ]
    events += [
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": f"token{index} "},
        }
        for index in range(chunks)
    ]
    if finish:
        events.append(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": chunks},
            }
        )
        events.append({"type": "message_stop"})
    return events


def _completion(anthropic: bool) -> dict[str, Any]:
    if anthropic:
        return {
            "id": "msg-fake",
            "model": "fake-model",
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 11, "output_tokens": 1},
        }
    return {
        "id": "chatcmpl-fake",
        "model": "fake-model",
        "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 1},
    }


class FakeModelServer:
    """An OpenAI-compatible (or, with ``anthropic=True``, Anthropic) API on 127.0.0.1."""

    def __init__(
        self,
        *,
        anthropic: bool = False,
        chunks: int = 2,
        finish: bool = False,
        delay: float | None = None,
    ) -> None:
        self.anthropic = anthropic
        self.chunks = chunks
        self.finish = finish
        self.delay = delay
        self.exchanges: list[Exchange] = []
        self.request_received = asyncio.Event()
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task[None]] = set()

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.sockets[0].getsockname()[:2]
        return f"http://{host}:{port}"

    async def __aenter__(self) -> FakeModelServer:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_: object) -> None:
        assert self._server is not None
        self._server.close()
        for task in tuple(self._handlers):
            task.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)
        await self._server.wait_closed()

    @property
    def last(self) -> Exchange:
        return self.exchanges[-1]

    async def wait_for_request(self, *, first_chunk: bool = True, within: float = 5) -> Exchange:
        """The newest exchange, once the request arrived (and its first chunk went out)."""
        await asyncio.wait_for(self.request_received.wait(), within)
        exchange = self.last
        if first_chunk and exchange.body.get("stream"):
            await asyncio.wait_for(exchange.first_chunk_sent.wait(), within)
        return exchange

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._handlers.add(task)
        try:
            await self._serve(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._handlers.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # One request per connection (every response says ``Connection: close``).
        head = await reader.readuntil(b"\r\n\r\n")
        request_line, *header_lines = head.decode("latin-1").split("\r\n")
        headers = {
            name.strip().lower(): value.strip()
            for name, _, value in (line.partition(":") for line in header_lines if line)
        }
        length = int(headers.get("content-length", "0"))
        raw = await reader.readexactly(length) if length else b""
        exchange = Exchange(
            path=request_line.split(" ")[1],
            body=json.loads(raw) if raw else {},
            headers=headers,
        )
        self.exchanges.append(exchange)
        self.request_received.set()
        # The client's EOF (or anything it sends) from now on means it has hung up.
        watcher = asyncio.create_task(self._watch(reader, exchange))
        try:
            if exchange.body.get("stream"):
                await self._stream(writer, exchange)
            else:
                await self._answer(writer, exchange)
        except ConnectionError:
            exchange.hang_up()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def _watch(self, reader: asyncio.StreamReader, exchange: Exchange) -> None:
        with contextlib.suppress(ConnectionError):
            await reader.read(1)
        exchange.hang_up()

    async def _stall(self, exchange: Exchange) -> None:
        await exchange.disconnected.wait()

    async def _answer(self, writer: asyncio.StreamWriter, exchange: Exchange) -> None:
        if self.delay is None:
            await self._stall(exchange)
            return
        delay = asyncio.create_task(asyncio.sleep(self.delay))
        hung_up = asyncio.create_task(exchange.disconnected.wait())
        done, pending = await asyncio.wait({delay, hung_up}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if hung_up in done:
            return
        body = json.dumps(_completion(self.anthropic)).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        exchange.finished = True

    async def _stream(self, writer: asyncio.StreamWriter, exchange: Exchange) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        )
        events = (
            _anthropic_events(self.chunks, self.finish)
            if self.anthropic
            else _openai_events(self.chunks, self.finish)
        )
        for event in events:
            self._chunk(writer, f"data: {json.dumps(event)}\n\n".encode())
        await writer.drain()
        exchange.first_chunk_sent.set()
        if not self.finish:
            await self._stall(exchange)
            return
        if not self.anthropic:
            self._chunk(writer, b"data: [DONE]\n\n")
        writer.write(b"0\r\n\r\n")
        await writer.drain()
        exchange.finished = True

    @staticmethod
    def _chunk(writer: asyncio.StreamWriter, data: bytes) -> None:
        writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")


class _TrackedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, transport: TrackingTransport) -> None:
        self._stream = stream
        self._transport = transport
        self._closed = False

    def __aiter__(self) -> AsyncIterator[bytes]:
        # The transport's own iterator: no generator of ours for the collector to finalize.
        return self._stream.__aiter__()

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self._transport.open_responses -= 1
        await self._stream.aclose()


class TrackingTransport(httpx.AsyncBaseTransport):
    """A real HTTP transport that counts the responses its client has not closed yet."""

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self.open_responses = 0
        self.requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        response = await self._inner.handle_async_request(request)
        self.open_responses += 1
        assert isinstance(response.stream, httpx.AsyncByteStream)
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_TrackedStream(response.stream, self),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def tracked_client() -> tuple[httpx.AsyncClient, TrackingTransport]:
    transport = TrackingTransport()
    return httpx.AsyncClient(transport=transport), transport


async def no_tasks_left(within: float = 1.0) -> None:
    """Assert that no task but the caller's is left once short-lived ones have finished.

    httpx's own line and byte readers, left suspended when a stream is closed early, are
    finished by the event loop's generator finalizer in tasks of their own, which close the
    socket and end within a few loop turns. A task still there after ``within`` seconds (a
    provider call or a reader waiting on the socket) is a leak.
    """
    current = asyncio.current_task()
    for _ in range(3):  # a finishing task may start another (a finalizer's own)
        if others := asyncio.all_tasks() - {current}:
            await asyncio.wait(others, timeout=within)
    others = asyncio.all_tasks() - {current}
    assert not others, others
