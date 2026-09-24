from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from algen_agent_runtime.api.body_limit import (
    RequestBodyLimitMiddleware,
)


async def dummy_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    request = Request(scope, receive)
    body = await request.body()
    response = JSONResponse({"received_bytes": len(body), "status": "ok"})
    await response(scope, receive, send)


async def call_asgi(
    app: Any,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]],
    chunks: list[bytes],
) -> tuple[int, dict[str, str], bytes]:
    status_code = 0
    response_headers: dict[str, str] = {}
    response_body = bytearray()
    chunks_queue = list(chunks)
    chunks_read = 0

    async def receive() -> dict[str, Any]:
        nonlocal chunks_read
        if chunks_queue:
            body = chunks_queue.pop(0)
            chunks_read += 1
            return {
                "type": "http.request",
                "body": body,
                "more_body": bool(chunks_queue),
            }
        return {
            "type": "http.request",
            "body": b"",
            "more_body": False,
        }

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = message["status"]
            for k, v in message.get("headers", ()):
                response_headers[k.decode("latin-1").lower()] = v.decode("latin-1")
        elif message["type"] == "http.response.body":
            response_body.extend(message.get("body", b""))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
    }

    await app(scope, receive, send)
    return status_code, response_headers, bytes(response_body)


@pytest.mark.asyncio
async def test_no_content_length_chunks_exceed_limit_rejected_413() -> None:
    middleware = RequestBodyLimitMiddleware(
        dummy_app, max_request_bytes=1000, max_artifact_bytes=5000
    )
    # 3 chunks of 400 bytes = 1200 bytes > 1000
    chunks = [b"a" * 400, b"b" * 400, b"c" * 400]
    status_code, _, body = await call_asgi(
        middleware, "POST", "/v1/runs", [(b"host", b"test")], chunks
    )
    assert status_code == 413
    payload = json.loads(body.decode("utf-8"))
    assert payload["code"] == "REQUEST_PAYLOAD_TOO_LARGE"
    assert payload["limit_bytes"] == 1000
    assert payload["detail"] == "request payload too large"


@pytest.mark.asyncio
async def test_declared_small_length_but_actual_chunks_exceed_limit_rejected_413() -> None:
    middleware = RequestBodyLimitMiddleware(
        dummy_app, max_request_bytes=1000, max_artifact_bytes=5000
    )
    # Falsified small Content-Length = 100, but chunks total 1200 bytes
    headers = [(b"host", b"test"), (b"content-length", b"100")]
    chunks = [b"x" * 600, b"y" * 600]
    status_code, _, body = await call_asgi(middleware, "POST", "/v1/runs", headers, chunks)
    assert status_code == 413
    payload = json.loads(body.decode("utf-8"))
    assert payload["code"] == "REQUEST_PAYLOAD_TOO_LARGE"
    assert payload["limit_bytes"] == 1000


@pytest.mark.asyncio
async def test_declared_length_exceeds_limit_before_body_read_413() -> None:
    body_read = False

    async def checking_app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal body_read
        body_read = True
        response = Response(b"ok", status_code=200)
        await response(scope, receive, send)

    middleware = RequestBodyLimitMiddleware(
        checking_app, max_request_bytes=1000, max_artifact_bytes=5000
    )
    headers = [(b"host", b"test"), (b"content-length", b"2000")]
    status_code, _, body = await call_asgi(middleware, "POST", "/v1/runs", headers, [b"x" * 2000])
    assert status_code == 413
    assert not body_read, "App should not have been called when declared size exceeds limit"
    payload = json.loads(body.decode("utf-8"))
    assert payload["code"] == "REQUEST_PAYLOAD_TOO_LARGE"
    assert payload["limit_bytes"] == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_cl", [b"-1", b"abc", b"12.5", b"", b"10, 20"])
async def test_invalid_content_length_rejected_400(invalid_cl: bytes) -> None:
    middleware = RequestBodyLimitMiddleware(
        dummy_app, max_request_bytes=1000, max_artifact_bytes=5000
    )
    headers = [(b"host", b"test"), (b"content-length", invalid_cl)]
    status_code, _, body = await call_asgi(middleware, "POST", "/v1/runs", headers, [b"test"])
    assert status_code == 400
    payload = json.loads(body.decode("utf-8"))
    assert payload["code"] == "INVALID_CONTENT_LENGTH"


@pytest.mark.asyncio
async def test_exact_limit_accepted() -> None:
    middleware = RequestBodyLimitMiddleware(
        dummy_app, max_request_bytes=1000, max_artifact_bytes=5000
    )
    headers = [(b"host", b"test"), (b"content-length", b"1000")]
    chunks = [b"a" * 500, b"b" * 500]
    status_code, _, body = await call_asgi(middleware, "POST", "/v1/runs", headers, chunks)
    assert status_code == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["received_bytes"] == 1000


@pytest.mark.asyncio
async def test_small_json_request_normal_execution() -> None:
    middleware = RequestBodyLimitMiddleware(
        dummy_app, max_request_bytes=1000, max_artifact_bytes=5000
    )
    req_json = b'{"input": "hello"}'
    headers = [(b"host", b"test"), (b"content-length", str(len(req_json)).encode())]
    status_code, _, body = await call_asgi(middleware, "POST", "/v1/runs", headers, [req_json])
    assert status_code == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["received_bytes"] == len(req_json)


@pytest.mark.asyncio
async def test_oversized_artifact_uses_max_artifact_bytes() -> None:
    middleware = RequestBodyLimitMiddleware(
        dummy_app, max_request_bytes=1000, max_artifact_bytes=5000
    )
    # An artifact request of 3000 bytes is permitted (limit 5000)
    headers = [(b"host", b"test"), (b"content-length", b"3000")]
    status_code, _, _body = await call_asgi(
        middleware, "POST", "/v1/artifacts", headers, [b"x" * 3000]
    )
    assert status_code == 200

    # But 6000 bytes exceeds artifact limit (5000)
    headers_over = [(b"host", b"test"), (b"content-length", b"6000")]
    status_code_over, _, body_over = await call_asgi(
        middleware, "POST", "/v1/artifacts", headers_over, [b"x" * 6000]
    )
    assert status_code_over == 413
    payload = json.loads(body_over.decode("utf-8"))
    assert payload["code"] == "REQUEST_PAYLOAD_TOO_LARGE"
    assert payload["limit_bytes"] == 5000


@pytest.mark.asyncio
async def test_error_response_contains_stable_schema_never_echoes_body() -> None:
    middleware = RequestBodyLimitMiddleware(
        dummy_app, max_request_bytes=100, max_artifact_bytes=500
    )
    secret_content = b"TOP_SECRET_PROMPT_DO_NOT_LEAK" * 10
    status_code, _, body = await call_asgi(
        middleware, "POST", "/v1/runs", [(b"host", b"test")], [secret_content]
    )
    assert status_code == 413
    raw_body = body.decode("utf-8")
    assert "TOP_SECRET" not in raw_body
    payload = json.loads(raw_body)
    assert set(payload.keys()) == {"detail", "code", "limit_bytes"}
    assert payload["code"] == "REQUEST_PAYLOAD_TOO_LARGE"
    assert payload["limit_bytes"] == 100
