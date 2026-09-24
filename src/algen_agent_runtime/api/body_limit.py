from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

from starlette.exceptions import HTTPException


class RequestPayloadTooLarge(HTTPException):
    def __init__(self, limit: int) -> None:
        super().__init__(
            status_code=413,
            detail="request payload too large",
        )
        self.limit = limit


class RequestBodyLimitMiddleware:
    def __init__(
        self,
        app: Any,
        max_request_bytes: int = 1_048_576,
        max_artifact_bytes: int = 10_485_760,
    ) -> None:
        self.app = app
        self.max_request_bytes = max_request_bytes
        self.max_artifact_bytes = max_artifact_bytes

    def _limit_for(self, method: str, path: str) -> int:
        if method == "POST" and path.rstrip("/") == "/v1/artifacts":
            return self.max_artifact_bytes
        return self.max_request_bytes

    @staticmethod
    def _parse_content_length(
        headers: Sequence[tuple[bytes, bytes]],
    ) -> tuple[bool, int | None]:
        cl_headers = [v for k, v in headers if k.lower() == b"content-length"]
        if not cl_headers:
            return True, None
        if len(cl_headers) > 1:
            return False, None
        raw = cl_headers[0].decode("latin-1").strip()
        if not raw.isdigit():
            return False, None
        try:
            val = int(raw)
            if val < 0:
                return False, None
            return True, val
        except ValueError:
            return False, None

    @staticmethod
    async def _send_error(
        send: Any,
        status_code: int,
        code: str,
        detail: str,
        limit_bytes: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {"detail": detail, "code": code}
        if limit_bytes is not None:
            payload["limit_bytes"] = limit_bytes
        body = json.dumps(payload).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            }
        )

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        path = scope.get("path", "")
        limit = self._limit_for(method, path)

        is_valid, declared = self._parse_content_length(scope.get("headers", ()))
        if not is_valid:
            await self._send_error(
                send, 400, "INVALID_CONTENT_LENGTH", "invalid Content-Length header"
            )
            return

        if declared is not None and declared > limit:
            await self._send_error(
                send,
                413,
                "REQUEST_PAYLOAD_TOO_LARGE",
                "request payload too large",
                limit_bytes=limit,
            )
            return

        consumed = 0
        response_started = False

        async def limited_receive() -> dict[str, Any]:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                consumed += len(body)
                if consumed > limit:
                    raise RequestPayloadTooLarge(limit)
            return cast(dict[str, Any], message)

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, send_wrapper)
        except RequestPayloadTooLarge as exc:
            if not response_started:
                await self._send_error(
                    send,
                    413,
                    "REQUEST_PAYLOAD_TOO_LARGE",
                    "request payload too large",
                    limit_bytes=exc.limit,
                )
