from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

from algen_agent_runtime.frameworks.contracts import (
    FrameworkCapabilities,
    FrameworkEventType,
    FrameworkRunRequest,
    FrameworkRunResult,
    FrameworkRunStatus,
    FrameworkStreamEvent,
)
from algen_agent_runtime.types.contracts import TokenUsage


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    content = getattr(value, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(value, Mapping):
        for key in ("output", "answer", "result", "content", "messages"):
            if key in value:
                return _text(value[key])
        for nested in value.values():
            if isinstance(nested, Mapping):
                for key in ("output", "answer", "result", "content", "messages"):
                    if key in nested:
                        return _text(nested[key])
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return _text(value[-1]) if value else ""
    return str(value)


def _usage(value: Any) -> TokenUsage:
    if value is None:
        return TokenUsage()
    if isinstance(value, Mapping):
        prompt = value.get("prompt_tokens", value.get("input_tokens", 0))
        completion = value.get("completion_tokens", value.get("output_tokens", 0))
    else:
        prompt = getattr(value, "prompt_tokens", getattr(value, "input_tokens", 0))
        completion = getattr(value, "completion_tokens", getattr(value, "output_tokens", 0))
    return TokenUsage(input_tokens=int(prompt or 0), output_tokens=int(completion or 0))


class _BaseAdapter:
    _active: dict[str, asyncio.Task[Any]]

    def __init__(self) -> None:
        self._active = {}

    async def cancel(self, run_id: str) -> bool:
        task = self._active.get(run_id)
        if not task or task.done():
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._active.pop(run_id, None)
        return True

    async def _tracked(self, run_id: str, awaitable: Any) -> Any:
        task = asyncio.ensure_future(awaitable)
        self._active[run_id] = task
        try:
            return await task
        finally:
            self._active.pop(run_id, None)


class LangGraphAdapter(_BaseAdapter):
    """Adapts a compiled LangGraph graph without importing LangGraph at module import time."""

    framework_id = "langgraph"
    capabilities = FrameworkCapabilities(
        streaming=True, cancellation=True, persistence=True, human_in_the_loop=True
    )

    def __init__(
        self,
        graph: Any,
        *,
        input_factory: Callable[[FrameworkRunRequest], Any] | None = None,
        output_selector: Callable[[Any], str] = _text,
    ) -> None:
        super().__init__()
        self._graph = graph
        self._input_factory = input_factory or (
            lambda request: {"messages": [{"role": "user", "content": request.input}]}
        )
        self._output_selector = output_selector

    def _config(self, request: FrameworkRunRequest) -> dict[str, Any]:
        supplied = dict(request.context.get("langgraph_config", {}))
        configurable = dict(supplied.get("configurable", {}))
        configurable.setdefault("thread_id", request.session_id)
        supplied["configurable"] = configurable
        supplied.setdefault("metadata", {})
        supplied["metadata"].update(
            {"run_id": request.run_id, "tenant_id": request.tenant_id, "user_id": request.user_id}
        )
        return supplied

    async def invoke(self, request: FrameworkRunRequest) -> FrameworkRunResult:
        result = await self._tracked(
            request.run_id,
            self._graph.ainvoke(self._input_factory(request), config=self._config(request)),
        )
        interruptions = getattr(result, "interrupts", ())
        value = getattr(result, "value", result)
        return FrameworkRunResult(
            output=self._output_selector(value),
            status=(
                FrameworkRunStatus.AWAITING_INPUT if interruptions else FrameworkRunStatus.COMPLETED
            ),
            metadata={"interruptions": [_text(item) for item in interruptions]},
            resume_state=result if interruptions else None,
        )

    async def stream(self, request: FrameworkRunRequest) -> AsyncIterator[FrameworkStreamEvent]:
        yield FrameworkStreamEvent(type=FrameworkEventType.STARTED)
        last: Any = None
        async for chunk in self._graph.astream(
            self._input_factory(request), config=self._config(request), stream_mode="updates"
        ):
            last = chunk
            yield FrameworkStreamEvent(type=FrameworkEventType.STEP, data={"update": chunk})
        result = FrameworkRunResult(output=self._output_selector(last))
        yield FrameworkStreamEvent(type=FrameworkEventType.COMPLETED, result=result)


class OpenAIAgentsAdapter(_BaseAdapter):
    framework_id = "openai_agents"
    capabilities = FrameworkCapabilities(
        streaming=True,
        cancellation=True,
        persistence=False,
        human_in_the_loop=True,
        multi_agent=True,
    )

    def __init__(self, agent: Any, *, runner: Any | None = None, run_config: Any = None) -> None:
        super().__init__()
        if runner is None:
            try:
                from agents import Runner
            except ImportError as exc:
                raise ImportError("install algen-agent-runtime[openai-agents]") from exc
            runner = Runner
        self._agent = agent
        self._runner = runner
        self._run_config = run_config

    def _kwargs(self, request: FrameworkRunRequest) -> dict[str, Any]:
        options = dict(request.context.get("openai_agents_options", {}))

        # Propagate identity and tenancy to documented context and session boundary
        ctx = dict(request.context)
        ctx.pop("openai_agents_options", None)
        ctx.setdefault("run_id", request.run_id)
        ctx.setdefault("tenant_id", request.tenant_id)
        ctx.setdefault("user_id", request.user_id)
        ctx.setdefault("session_id", request.session_id)
        options.setdefault("context", ctx)
        options.setdefault("conversation_id", request.session_id)

        # Forward RunConfig with tracing_disabled=True and trace metadata
        if "run_config" not in options:
            if self._run_config is not None:
                options["run_config"] = self._run_config
            else:
                try:
                    from agents import RunConfig

                    options["run_config"] = RunConfig(
                        tracing_disabled=True,
                        trace_id=request.run_id,
                        trace_metadata={
                            "run_id": request.run_id,
                            "tenant_id": request.tenant_id,
                            "user_id": request.user_id,
                            "session_id": request.session_id,
                        },
                    )
                except ImportError:
                    pass
        return options

    async def invoke(self, request: FrameworkRunRequest) -> FrameworkRunResult:
        result = await self._tracked(
            request.run_id,
            self._runner.run(self._agent, request.input, **self._kwargs(request)),
        )
        raw_interruptions = tuple(getattr(result, "interruptions", ()))
        interruption_summary = [
            {
                "type": str(getattr(i, "type", "interruption")),
                "tool_name": str(getattr(i, "tool_name", "")),
            }
            for i in raw_interruptions
        ]
        return FrameworkRunResult(
            output=_text(getattr(result, "final_output", result)),
            status=(
                FrameworkRunStatus.AWAITING_INPUT
                if raw_interruptions
                else FrameworkRunStatus.COMPLETED
            ),
            usage=_usage(getattr(getattr(result, "context_wrapper", None), "usage", None)),
            metadata={
                "interruption_count": len(raw_interruptions),
                "interruptions": interruption_summary,
            },
            resume_state=None,
        )

    async def stream(self, request: FrameworkRunRequest) -> AsyncIterator[FrameworkStreamEvent]:
        current = asyncio.current_task()
        if current is not None:
            self._active[request.run_id] = current
        try:
            yield FrameworkStreamEvent(type=FrameworkEventType.STARTED)
            streamed = self._runner.run_streamed(
                self._agent, request.input, **self._kwargs(request)
            )
            async for event in streamed.stream_events():
                data = getattr(event, "data", None)
                delta = getattr(data, "delta", None)
                if isinstance(delta, str):
                    yield FrameworkStreamEvent(type=FrameworkEventType.DELTA, delta=delta)
                else:
                    yield FrameworkStreamEvent(
                        type=FrameworkEventType.STEP,
                        data={"event_type": str(getattr(event, "type", "event"))},
                    )
            result = FrameworkRunResult(
                output=_text(getattr(streamed, "final_output", "")),
                usage=_usage(getattr(getattr(streamed, "context_wrapper", None), "usage", None)),
            )
            yield FrameworkStreamEvent(type=FrameworkEventType.COMPLETED, result=result)
        finally:
            self._active.pop(request.run_id, None)

    async def cancel(self, run_id: str) -> bool:
        return await super().cancel(run_id)


class AutoGenAdapter(_BaseAdapter):
    framework_id = "autogen"
    capabilities = FrameworkCapabilities(
        streaming=True,
        cancellation=True,
        persistence=True,
        human_in_the_loop=True,
        multi_agent=True,
    )

    def __init__(self, agent_or_team: Any) -> None:
        super().__init__()
        self._target = agent_or_team

    async def invoke(self, request: FrameworkRunRequest) -> FrameworkRunResult:
        result = await self._tracked(request.run_id, self._target.run(task=request.input))
        messages = getattr(result, "messages", ())
        final = messages[-1] if messages else result
        usage = TokenUsage()
        for message in messages:
            item_usage = _usage(getattr(message, "models_usage", None))
            usage = TokenUsage(
                input_tokens=usage.input_tokens + item_usage.input_tokens,
                output_tokens=usage.output_tokens + item_usage.output_tokens,
            )
        return FrameworkRunResult(
            output=_text(final),
            usage=usage,
            metadata={"stop_reason": getattr(result, "stop_reason", None)},
        )

    async def stream(self, request: FrameworkRunRequest) -> AsyncIterator[FrameworkStreamEvent]:
        yield FrameworkStreamEvent(type=FrameworkEventType.STARTED)
        final: Any = None
        async for item in self._target.run_stream(task=request.input):
            if hasattr(item, "messages"):
                final = item
                continue
            content = getattr(item, "content", None)
            event_type = type(item).__name__
            if event_type == "ModelClientStreamingChunkEvent" and isinstance(content, str):
                yield FrameworkStreamEvent(type=FrameworkEventType.DELTA, delta=content)
            else:
                yield FrameworkStreamEvent(
                    type=FrameworkEventType.STEP,
                    data={"event_type": event_type, "source": getattr(item, "source", None)},
                )
        messages = getattr(final, "messages", ())
        result = FrameworkRunResult(output=_text(messages[-1] if messages else final))
        yield FrameworkStreamEvent(type=FrameworkEventType.COMPLETED, result=result)


class CrewAIAdapter(_BaseAdapter):
    framework_id = "crewai"
    capabilities = FrameworkCapabilities(cancellation=True, multi_agent=True)

    def __init__(
        self,
        crew: Any,
        *,
        input_factory: Callable[[FrameworkRunRequest], Mapping[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self._crew = crew
        self._input_factory = input_factory or (lambda request: {"input": request.input})

    async def invoke(self, request: FrameworkRunRequest) -> FrameworkRunResult:
        inputs = dict(self._input_factory(request))
        kickoff_async = getattr(self._crew, "kickoff_async", None)
        if kickoff_async is not None:
            result = await self._tracked(request.run_id, kickoff_async(inputs=inputs))
        else:
            result = await self._tracked(
                request.run_id, asyncio.to_thread(self._crew.kickoff, inputs=inputs)
            )
        usage_value = getattr(result, "token_usage", None)
        return FrameworkRunResult(
            output=_text(getattr(result, "raw", result)),
            usage=_usage(usage_value),
            metadata={"framework_output_type": type(result).__name__},
        )

    async def stream(self, request: FrameworkRunRequest) -> AsyncIterator[FrameworkStreamEvent]:
        yield FrameworkStreamEvent(type=FrameworkEventType.STARTED)
        result = await self.invoke(request)
        yield FrameworkStreamEvent(type=FrameworkEventType.COMPLETED, result=result)
