from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping

from algen_agent_runtime.events.contracts import STREAM_END_EVENT_TYPES, RunEvent
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.types.contracts import RunRequest, RunResult


class AlgenAgentRuntimeClient:
    """Library facade for synchronous, asynchronous, and event-streaming execution."""

    def __init__(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime

    @property
    def tools(self) -> ToolRegistry:
        """The runtime's tool registry, for workflow ``tool:`` steps."""
        return self._runtime.tools

    @property
    def tool_executor(self) -> ToolExecutor:
        """The runtime's tool executor, for workflow ``tool:`` steps."""
        return self._runtime.tool_executor

    async def run(
        self, request: RunRequest, *, credentials: Mapping[str, str] | None = None
    ) -> RunResult:
        """Run ``request`` and wait for it.

        ``credentials`` are the caller's (for example ``{"authorization": "Bearer ..."}``), as for
        ``AgentRuntime.start``: tools read them from ``ToolContext.credentials``.
        """
        return await self._runtime.run(request, credentials=credentials)

    def run_sync(
        self, request: RunRequest, *, credentials: Mapping[str, str] | None = None
    ) -> RunResult:
        """``run`` from synchronous code; ``credentials`` as for ``run``."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run(request, credentials=credentials))
        raise RuntimeError("run_sync cannot be called from an active event loop; await run instead")

    async def stream(
        self, request: RunRequest, *, credentials: Mapping[str, str] | None = None
    ) -> AsyncIterator[RunEvent]:
        """Start a run and yield its events until it ends or pauses.

        ``credentials`` as for ``run``.

        The last event is ``run.completed``, ``run.failed`` or ``run.cancelled`` when the run
        ended, or ``approval.required``, ``clarification.required`` or
        ``tool.reconciliation_required`` when it paused (``STREAM_END_EVENT_TYPES``). After a
        pause, answer it (``AgentRuntime.resume``, say) and follow the run with
        ``stream_events(run_id, after=last_event.sequence)``; the resumed run sees only the
        credentials passed to ``resume``.
        """
        state = await self._runtime.start(
            request.model_copy(update={"stream": True}), credentials=credentials
        )
        async for event in self.stream_events(state.id):
            yield event

    async def stream_events(self, run_id: str, *, after: int = 0) -> AsyncIterator[RunEvent]:
        """Yield an existing run's events after sequence ``after``, until it ends or pauses,
        as ``stream`` does."""
        async for event in self._runtime.events.subscribe(run_id, after=after):
            yield event
            if event.type in STREAM_END_EVENT_TYPES:
                return
