from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from algen_agent_runtime.events.contracts import RunEvent
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.types.contracts import RunRequest, RunResult


class AlgenAgentRuntimeClient:
    """Library facade for synchronous, asynchronous, and event-streaming execution."""

    def __init__(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime

    async def run(self, request: RunRequest) -> RunResult:
        return await self._runtime.run(request)

    def run_sync(self, request: RunRequest) -> RunResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run(request))
        raise RuntimeError("run_sync cannot be called from an active event loop; await run instead")

    async def stream(self, request: RunRequest) -> AsyncIterator[RunEvent]:
        state = await self._runtime.start(request.model_copy(update={"stream": True}))
        async for event in self._runtime.events.subscribe(state.id):
            yield event
            if event.type in {"run.completed", "run.failed", "run.cancelled"}:
                return
