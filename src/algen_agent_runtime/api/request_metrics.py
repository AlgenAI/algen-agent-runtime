"""HTTP request metrics: ``algen_agent_runtime.api.requests`` and ``api.request.duration``.

Labelled by method, route template (``/v1/runs/{run_id}``, never the concrete path) and status
code, so the run-admission SLO can be computed from the Runtime's own metrics.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from starlette.routing import Match

from algen_agent_runtime.observability.metrics import instrument

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: The ``route`` attribute of a request no route matched (a 404, or a path refused before routing
#: whose template cannot be found).
UNMATCHED_ROUTE = "unmatched"


class RequestMetricsMiddleware:
    """Count each HTTP request and time it to its response's start."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._requests = instrument("algen_agent_runtime.api.requests")
        self._duration = instrument("algen_agent_runtime.api.request.duration")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status: list[int] = []

        async def observed_send(message: Message) -> None:
            if message["type"] == "http.response.start" and not status:
                status.append(int(message["status"]))
                self._observe(scope, status[0], started)
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except BaseException:
            if not status:
                status.append(500)
                self._observe(scope, 500, started)
            raise

    def _observe(self, scope: Scope, status_code: int, started: float) -> None:
        attributes = {
            "method": str(scope.get("method", "")),
            "route": _route_template(scope),
            "status_code": str(status_code),
        }
        try:
            self._requests.add(1, attributes)
            self._duration.record(time.monotonic() - started, attributes)
        except Exception:
            return


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str):
        return path
    # Refused before routing (an admission limit, an oversized body): find the template.
    app = scope.get("app")
    for candidate in getattr(getattr(app, "router", None), "routes", ()):
        try:
            match, _ = candidate.matches(scope)
        except Exception:
            continue
        if match == Match.FULL:
            template = getattr(candidate, "path", None)
            if isinstance(template, str):
                return template
    return UNMATCHED_ROUTE
