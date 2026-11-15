"""Adapters that run an agent built with a foreign framework behind the Runtime's contract.

Every adapter, since 0.2.0b4:

- returns a ``FAILED`` result with ``metadata["error"] = {"type", "message"}`` (the message
  redacted and bounded) instead of raising the framework's exception, and a ``CANCELLED`` result
  when :meth:`cancel` stopped the run; ``stream`` ends with a ``FAILED`` event carrying that
  result. A cancellation of the caller's own task is still raised.
- sets the run's identity itself: a ``request.context`` that names another tenant, user, run,
  session, thread or conversation is refused (``FAILED``, ``PolicyDeniedError``).
- registers ``stream`` as well as ``invoke`` for :meth:`cancel`.
- reports a pause (a LangGraph interrupt, an OpenAI Agents tool approval, an AutoGen handoff to
  the user) as ``AWAITING_INPUT`` with ``metadata["interrupts"] = [{"id", "value"}]``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any

from algen_agent_runtime.exceptions.errors import PolicyDeniedError
from algen_agent_runtime.frameworks.contracts import (
    FrameworkCapabilities,
    FrameworkEventType,
    FrameworkRunRequest,
    FrameworkRunResult,
    FrameworkRunStatus,
    FrameworkStreamEvent,
)
from algen_agent_runtime.types.contracts import TokenUsage

#: Longest error message a FAILED result carries.
_ERROR_MESSAGE_LIMIT = 500
_IDENTITY_KEYS = ("run_id", "tenant_id", "user_id", "session_id")


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


def _add(total: TokenUsage, item: TokenUsage) -> TokenUsage:
    return TokenUsage(
        input_tokens=total.input_tokens + item.input_tokens,
        output_tokens=total.output_tokens + item.output_tokens,
    )


def _error(exc: BaseException) -> dict[str, str]:
    """An exception as a FAILED result reports it: its type and a redacted, bounded message."""
    from algen_agent_runtime.security.redaction import redact_secrets

    message = str(redact_secrets(str(exc)))
    if len(message) > _ERROR_MESSAGE_LIMIT:
        message = message[: _ERROR_MESSAGE_LIMIT - 1] + "…"
    return {"type": type(exc).__name__, "message": message}


def _failed(exc: BaseException) -> FrameworkRunResult:
    return FrameworkRunResult(
        output="", status=FrameworkRunStatus.FAILED, metadata={"error": _error(exc)}
    )


def _cancelled() -> FrameworkRunResult:
    return FrameworkRunResult(output="", status=FrameworkRunStatus.CANCELLED)


def _terminal(result: FrameworkRunResult) -> FrameworkStreamEvent:
    """The last event of a stream: COMPLETED for a completed or paused run, FAILED for a failed
    or cancelled one; ``result.status`` says which."""
    failed = result.status in {FrameworkRunStatus.FAILED, FrameworkRunStatus.CANCELLED}
    return FrameworkStreamEvent(
        type=FrameworkEventType.FAILED if failed else FrameworkEventType.COMPLETED, result=result
    )


def _refuse_other_identity(request: FrameworkRunRequest, supplied: Mapping[str, Any]) -> None:
    """Refuse a caller-supplied mapping that names another run, tenant, user or session."""
    for key in _IDENTITY_KEYS:
        if key in supplied and supplied[key] != getattr(request, key):
            raise PolicyDeniedError(
                f"request.context sets {key} to another value than the request's; the adapter "
                "sets the run's identity itself"
            )


def _accepts(function: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == name or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _json_value(value: Any) -> Any:
    """A structured framework output as plain JSON data, or None when it is not one."""
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        with contextlib.suppress(Exception):
            return dump(mode="json")
    if isinstance(value, Mapping):
        try:
            return json.loads(json.dumps(value, default=str))
        except (TypeError, ValueError):
            return None
    return None


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class _BaseAdapter:
    _active: dict[str, asyncio.Task[Any]]

    def __init__(self) -> None:
        self._active = {}
        self._cancel_requested: set[str] = set()

    async def cancel(self, run_id: str) -> bool:
        """Stop the run's ``invoke`` or ``stream``; it then returns or ends with ``CANCELLED``.
        False when no such run is active."""
        task = self._active.get(run_id)
        if not task or task.done():
            return False
        self._cancel_requested.add(run_id)
        await self._before_cancel(run_id)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        if self._active.get(run_id) is task:
            self._active.pop(run_id, None)
        return True

    async def _before_cancel(self, run_id: str) -> None:
        """Ask the framework to stop cooperatively before its task is cancelled."""

    def _register(self, run_id: str, task: asyncio.Task[Any]) -> None:
        self._cancel_requested.discard(run_id)
        self._active[run_id] = task

    def _release(self, run_id: str, task: asyncio.Task[Any]) -> None:
        if self._active.get(run_id) is task:
            self._active.pop(run_id, None)

    async def _tracked(self, run_id: str, awaitable: Awaitable[Any]) -> Any:
        task = asyncio.ensure_future(awaitable)
        self._register(run_id, task)
        try:
            return await task
        finally:
            self._release(run_id, task)

    def _cancelled_by_adapter(self, run_id: str) -> bool:
        """Whether a CancelledError came from :meth:`cancel`, not from the caller's own task
        being cancelled (that one is raised, as asyncio requires)."""
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            return False
        if run_id in self._cancel_requested:
            self._cancel_requested.discard(run_id)
            return True
        return False

    async def _guarded(
        self, request: FrameworkRunRequest, run: Callable[[], Awaitable[FrameworkRunResult]]
    ) -> FrameworkRunResult:
        try:
            return await run()
        except asyncio.CancelledError:
            if self._cancelled_by_adapter(request.run_id):
                return _cancelled()
            raise
        except Exception as exc:
            return _failed(exc)

    async def _pump(
        self, run_id: str, source: Callable[[], AsyncGenerator[Any, None]]
    ) -> AsyncGenerator[Any, None]:
        """Items of ``source``, iterated in a task registered for :meth:`cancel`. The task's
        error (or CancelledError) is raised here after the items it produced."""
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)

        async def produce() -> None:
            async with contextlib.aclosing(source()) as items:
                async for item in items:
                    await queue.put(item)

        task = asyncio.ensure_future(produce())
        self._register(run_id, task)
        getter: asyncio.Future[Any] | None = None
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield getter.result()
                    continue
                getter.cancel()
                while not queue.empty():
                    yield queue.get_nowait()
                task.result()
                return
        finally:
            if getter is not None and not getter.done():
                getter.cancel()
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            self._release(run_id, task)

    async def _stream_guarded(
        self,
        request: FrameworkRunRequest,
        events: Callable[[list[FrameworkRunResult]], AsyncGenerator[FrameworkStreamEvent, None]],
    ) -> AsyncIterator[FrameworkStreamEvent]:
        """STARTED, the adapter's events, then one terminal event. ``events`` appends the run's
        result to the list it is given instead of yielding it."""
        yield FrameworkStreamEvent(type=FrameworkEventType.STARTED)
        results: list[FrameworkRunResult] = []
        try:
            async with contextlib.aclosing(
                self._pump(request.run_id, lambda: events(results))
            ) as pumped:
                async for event in pumped:
                    yield event
        except asyncio.CancelledError:
            if not self._cancelled_by_adapter(request.run_id):
                raise
            yield _terminal(_cancelled())
            return
        except Exception as exc:
            yield _terminal(_failed(exc))
            return
        yield _terminal(results[-1] if results else FrameworkRunResult(output=""))


# --------------------------------------------------------------------------------------------
# LangGraph
# --------------------------------------------------------------------------------------------


def _interrupt_item(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return {"id": item.get("id"), "value": item.get("value", item)}
    return {"id": getattr(item, "id", None), "value": getattr(item, "value", item)}


def _langgraph_interrupts(result: Any) -> tuple[Any, ...]:
    """Interrupts from LangGraph's default (v1) output (``"__interrupt__"`` key) or the v2
    output's ``.interrupts``."""
    if isinstance(result, Mapping):
        found = result.get("__interrupt__") or ()
    else:
        found = getattr(result, "interrupts", ()) or ()
    if isinstance(found, (str, bytes)) or not isinstance(found, Sequence):
        found = (found,)
    return tuple(found)


def _message_usage(message: Any) -> TokenUsage:
    if isinstance(message, Mapping):
        return _usage(message.get("usage_metadata"))
    return _usage(getattr(message, "usage_metadata", None))


def _message_id(message: Any) -> Any:
    return message.get("id") if isinstance(message, Mapping) else getattr(message, "id", None)


class LangGraphAdapter(_BaseAdapter):
    """Adapts a compiled LangGraph graph without importing LangGraph at module import time.

    The thread is always ``f"{tenant_id}:{session_id}"``: two tenants with the same session id
    never share a checkpoint, and ``request.context["langgraph_config"]`` cannot pick another
    thread. ``capabilities.persistence`` is True only when the graph has a checkpointer.
    """

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
        self.capabilities = FrameworkCapabilities(
            streaming=True,
            cancellation=True,
            persistence=self._checkpointed,
            human_in_the_loop=True,
        )

    @property
    def _checkpointed(self) -> bool:
        return getattr(self._graph, "checkpointer", None) not in (None, False)

    @staticmethod
    def thread_id(request: FrameworkRunRequest) -> str:
        """The LangGraph thread a request runs on: ``"<tenant_id>:<session_id>"``."""
        return f"{request.tenant_id}:{request.session_id}"

    def _config(self, request: FrameworkRunRequest) -> dict[str, Any]:
        supplied = dict(request.context.get("langgraph_config", {}))
        configurable = dict(supplied.get("configurable", {}))
        thread = self.thread_id(request)
        if configurable.get("thread_id", thread) != thread:
            raise PolicyDeniedError(
                "request.context['langgraph_config'] sets configurable.thread_id; the adapter "
                "runs every request on thread '<tenant_id>:<session_id>', so choose the thread "
                "with session_id"
            )
        configurable["thread_id"] = thread
        supplied["configurable"] = configurable
        metadata = dict(supplied.get("metadata", {}))
        _refuse_other_identity(request, metadata)
        metadata.update({key: getattr(request, key) for key in _IDENTITY_KEYS})
        supplied["metadata"] = metadata
        return supplied

    async def _prior_message_ids(self, config: dict[str, Any]) -> set[Any]:
        """Ids of the messages already in the thread, so usage counts only this run's."""
        get_state = getattr(self._graph, "aget_state", None)
        if not self._checkpointed or get_state is None:
            return set()
        try:
            snapshot = await get_state(config)
        except Exception:
            return set()
        values = getattr(snapshot, "values", None)
        messages = values.get("messages", ()) if isinstance(values, Mapping) else ()
        return {_message_id(message) for message in messages if _message_id(message)}

    def _result(self, final: Any, interrupts: Sequence[Any], prior: set[Any]) -> FrameworkRunResult:
        if isinstance(final, Mapping):
            value: Any = {key: item for key, item in final.items() if key != "__interrupt__"}
        else:
            value = getattr(final, "value", final)
        usage = TokenUsage()
        messages = value.get("messages", ()) if isinstance(value, Mapping) else ()
        if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
            for message in messages:
                identifier = _message_id(message)
                if identifier is None or identifier not in prior:
                    usage = _add(usage, _message_usage(message))
        metadata: dict[str, Any] = {
            "interruptions": [_text(getattr(item, "value", item)) for item in interrupts],
            "interrupts": [_interrupt_item(item) for item in interrupts],
            "checkpointer": self._checkpointed,
        }
        return FrameworkRunResult(
            output=self._output_selector(value),
            status=(
                FrameworkRunStatus.AWAITING_INPUT if interrupts else FrameworkRunStatus.COMPLETED
            ),
            usage=usage,
            metadata=metadata,
            resume_state=final if interrupts else None,
        )

    async def invoke(self, request: FrameworkRunRequest) -> FrameworkRunResult:
        async def run() -> FrameworkRunResult:
            config = self._config(request)
            prior = await self._prior_message_ids(config)
            result = await self._tracked(
                request.run_id,
                self._graph.ainvoke(self._input_factory(request), config=config),
            )
            return self._result(result, _langgraph_interrupts(result), prior)

        return await self._guarded(request, run)

    async def stream(self, request: FrameworkRunRequest) -> AsyncIterator[FrameworkStreamEvent]:
        """``updates`` become STEP events; the result is built from the final state (the last
        ``values`` chunk), as :meth:`invoke` builds it, and an interrupt makes it
        ``AWAITING_INPUT``."""

        async def events(
            results: list[FrameworkRunResult],
        ) -> AsyncGenerator[FrameworkStreamEvent, None]:
            config = self._config(request)
            prior = await self._prior_message_ids(config)
            final: Any = None
            last_update: Any = None
            # LangGraph reports an interrupt in the updates chunk and again in the values chunk
            # that follows: keep each one once, by id.
            interrupts: dict[Any, Any] = {}
            async for chunk in self._graph.astream(
                self._input_factory(request), config=config, stream_mode=["updates", "values"]
            ):
                if (
                    isinstance(chunk, tuple)
                    and len(chunk) == 2
                    and chunk[0] in {"updates", "values"}
                ):
                    mode, data = chunk
                else:  # a graph that ignores stream_mode and yields updates
                    mode, data = "updates", chunk
                if isinstance(data, Mapping) and "__interrupt__" in data:
                    for item in _langgraph_interrupts(data):
                        key = _interrupt_item(item)["id"] or id(item)
                        interrupts.setdefault(key, item)
                    if mode == "updates":
                        continue
                if mode == "values":
                    final = data
                    continue
                last_update = data
                yield FrameworkStreamEvent(type=FrameworkEventType.STEP, data={"update": data})
            if final is None:
                final = last_update
            results.append(self._result(final, list(interrupts.values()), prior))

        async for event in self._stream_guarded(request, events):
            yield event


# --------------------------------------------------------------------------------------------
# OpenAI Agents SDK
# --------------------------------------------------------------------------------------------

#: ``openai_agents_options`` keys a request may not set: they carry the run's identity, its
#: server-side conversation or its tracing, which the adapter (and its constructor) own.
_OPENAI_RESERVED_OPTIONS = frozenset(
    {"context", "conversation_id", "previous_response_id", "run_config", "session"}
)


def _arguments(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _openai_interruption(item: Any) -> dict[str, Any]:
    raw = getattr(item, "raw_item", None)

    def field(name: str) -> Any:
        value = getattr(item, name, None)
        if value is None and raw is not None:
            value = raw.get(name) if isinstance(raw, Mapping) else getattr(raw, name, None)
        return value

    call_id = field("call_id") or field("id")
    return {
        "type": str(getattr(item, "type", "interruption")),
        "tool_name": str(field("tool_name") or field("name") or ""),
        "call_id": None if call_id is None else str(call_id),
        "arguments": _arguments(field("arguments")),
    }


class OpenAIAgentsAdapter(_BaseAdapter):
    """Runs an OpenAI Agents SDK agent. The run's identity goes into the run ``context`` (which
    tools read) and the trace metadata.

    ``conversation_id`` is the id of an OpenAI server-managed conversation, so the adapter sends
    one only when the constructor's ``conversation_id`` callable returns one for the request:
    the host maps its tenant's session to a conversation it created for that tenant. Before
    0.2.0b4 the adapter sent ``session_id``, which the Responses API rejects unless it is such a
    conversation id. ``request.context["openai_agents_options"]`` may pass other ``Runner.run``
    options, but not ``context``, ``conversation_id``, ``previous_response_id``, ``session`` or
    ``run_config`` (set ``run_config`` on the constructor). An interrupted run keeps its
    ``RunResult`` in ``resume_state``."""

    framework_id = "openai_agents"
    capabilities = FrameworkCapabilities(
        streaming=True,
        cancellation=True,
        persistence=False,
        human_in_the_loop=True,
        multi_agent=True,
    )

    def __init__(
        self,
        agent: Any,
        *,
        runner: Any | None = None,
        run_config: Any = None,
        conversation_id: Callable[[FrameworkRunRequest], str | None] | None = None,
    ) -> None:
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
        self._conversation_id = conversation_id

    def _kwargs(self, request: FrameworkRunRequest) -> dict[str, Any]:
        options = dict(request.context.get("openai_agents_options", {}))
        reserved = sorted(_OPENAI_RESERVED_OPTIONS.intersection(options))
        if reserved:
            raise PolicyDeniedError(
                f"openai_agents_options may not set {reserved}: the adapter sets the run's "
                "context and conversation, and run_config is set on OpenAIAgentsAdapter"
            )
        ctx = {
            key: value for key, value in request.context.items() if key != "openai_agents_options"
        }
        _refuse_other_identity(request, ctx)
        ctx.update({key: getattr(request, key) for key in _IDENTITY_KEYS})
        options["context"] = ctx
        conversation = self._conversation_id(request) if self._conversation_id else None
        if conversation is not None:
            options["conversation_id"] = conversation
        if self._run_config is not None:
            options["run_config"] = self._run_config
        else:
            try:
                from agents import RunConfig

                options["run_config"] = RunConfig(
                    tracing_disabled=True,
                    trace_id=request.run_id,
                    trace_metadata={key: getattr(request, key) for key in _IDENTITY_KEYS},
                )
            except ImportError:
                pass
        return options

    def _result(self, result: Any, output: Any) -> FrameworkRunResult:
        raw = tuple(getattr(result, "interruptions", ()) or ())
        summary = [_openai_interruption(item) for item in raw]
        return FrameworkRunResult(
            output=_text(output),
            status=FrameworkRunStatus.AWAITING_INPUT if raw else FrameworkRunStatus.COMPLETED,
            usage=_usage(getattr(getattr(result, "context_wrapper", None), "usage", None)),
            metadata={
                "interruption_count": len(raw),
                "interruptions": summary,
                "interrupts": [
                    {
                        "id": item["call_id"],
                        "value": {"tool_name": item["tool_name"], "arguments": item["arguments"]},
                    }
                    for item in summary
                ],
            },
            resume_state=result if raw else None,
        )

    async def invoke(self, request: FrameworkRunRequest) -> FrameworkRunResult:
        async def run() -> FrameworkRunResult:
            kwargs = self._kwargs(request)
            result = await self._tracked(
                request.run_id, self._runner.run(self._agent, request.input, **kwargs)
            )
            return self._result(result, getattr(result, "final_output", result))

        return await self._guarded(request, run)

    async def stream(self, request: FrameworkRunRequest) -> AsyncIterator[FrameworkStreamEvent]:
        async def events(
            results: list[FrameworkRunResult],
        ) -> AsyncGenerator[FrameworkStreamEvent, None]:
            kwargs = self._kwargs(request)
            streamed = self._runner.run_streamed(self._agent, request.input, **kwargs)
            try:
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
            except asyncio.CancelledError:
                stop = getattr(streamed, "cancel", None)
                if callable(stop):
                    with contextlib.suppress(Exception):
                        stop()
                raise
            results.append(self._result(streamed, getattr(streamed, "final_output", "")))

        async for event in self._stream_guarded(request, events):
            yield event


# --------------------------------------------------------------------------------------------
# AutoGen AgentChat
# --------------------------------------------------------------------------------------------


def _cancellation_token() -> Any:
    try:
        from autogen_core import CancellationToken  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        return None
    return CancellationToken()


class AutoGenAdapter(_BaseAdapter):
    """Runs an AutoGen AgentChat agent or team.

    Pass ``factory`` (preferred): it builds a fresh agent or team for each request, so no
    conversation state is shared between tenants or sessions, and it receives the request, so it
    can give the agents the run's identity. A single ``agent_or_team`` instance is also accepted:
    its runs are then serialised and it is ``reset()`` before each one, so nothing carries over
    from one request to the next. Neither keeps a conversation across turns
    (``persistence=False``); a handoff to the user is reported as ``AWAITING_INPUT`` but cannot
    be resumed through the adapter yet (``human_in_the_loop=False``).
    """

    framework_id = "autogen"
    capabilities = FrameworkCapabilities(
        streaming=True,
        cancellation=True,
        persistence=False,
        human_in_the_loop=False,
        multi_agent=True,
    )

    def __init__(
        self,
        agent_or_team: Any = None,
        *,
        factory: Callable[[FrameworkRunRequest], Any] | None = None,
        input_factory: Callable[[FrameworkRunRequest], Any] | None = None,
        handoff_target: str = "user",
    ) -> None:
        super().__init__()
        if (agent_or_team is None) == (factory is None):
            raise ValueError("pass AutoGenAdapter either an agent_or_team or a factory")
        self._target = agent_or_team
        self._factory = factory
        self._input_factory = input_factory or (lambda request: request.input)
        self._handoff_target = handoff_target
        self._lock = asyncio.Lock()
        self._tokens: dict[str, Any] = {}

    async def _before_cancel(self, run_id: str) -> None:
        token = self._tokens.get(run_id)
        if token is not None:
            token.cancel()

    @contextlib.asynccontextmanager
    async def _target_for(self, request: FrameworkRunRequest) -> AsyncIterator[Any]:
        if self._factory is not None:
            yield await _maybe_await(self._factory(request))
            return
        async with self._lock:
            reset = getattr(self._target, "reset", None)
            if callable(reset):
                await _maybe_await(reset())
            yield self._target

    def _call_kwargs(self, request: FrameworkRunRequest, method: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"task": self._input_factory(request)}
        token = _cancellation_token()
        if token is not None and _accepts(method, "cancellation_token"):
            kwargs["cancellation_token"] = token
            self._tokens[request.run_id] = token
        return kwargs

    def _result(self, result: Any) -> FrameworkRunResult:
        messages = list(getattr(result, "messages", ()) or ())
        final = messages[-1] if messages else result
        usage = TokenUsage()
        for message in messages:
            usage = _add(usage, _usage(getattr(message, "models_usage", None)))
        metadata: dict[str, Any] = {"stop_reason": getattr(result, "stop_reason", None)}
        handoff = (
            final
            if messages
            and type(final).__name__ == "HandoffMessage"
            and getattr(final, "target", None) == self._handoff_target
            else None
        )
        if handoff is not None:
            metadata["interrupts"] = [
                {
                    "id": getattr(handoff, "id", None),
                    "value": {
                        "source": getattr(handoff, "source", None),
                        "target": getattr(handoff, "target", None),
                        "content": _text(handoff),
                    },
                }
            ]
        return FrameworkRunResult(
            output=_text(final),
            status=(FrameworkRunStatus.AWAITING_INPUT if handoff else FrameworkRunStatus.COMPLETED),
            usage=usage,
            metadata=metadata,
        )

    async def invoke(self, request: FrameworkRunRequest) -> FrameworkRunResult:
        async def run() -> FrameworkRunResult:
            try:
                async with self._target_for(request) as target:
                    kwargs = self._call_kwargs(request, target.run)
                    result = await self._tracked(request.run_id, target.run(**kwargs))
            finally:
                self._tokens.pop(request.run_id, None)
            return self._result(result)

        return await self._guarded(request, run)

    async def stream(self, request: FrameworkRunRequest) -> AsyncIterator[FrameworkStreamEvent]:
        async def events(
            results: list[FrameworkRunResult],
        ) -> AsyncGenerator[FrameworkStreamEvent, None]:
            final: Any = None
            try:
                async with self._target_for(request) as target:
                    kwargs = self._call_kwargs(request, target.run_stream)
                    async for item in target.run_stream(**kwargs):
                        if hasattr(item, "messages"):
                            final = item
                            continue
                        content = getattr(item, "content", None)
                        event_type = type(item).__name__
                        if event_type == "ModelClientStreamingChunkEvent" and isinstance(
                            content, str
                        ):
                            yield FrameworkStreamEvent(type=FrameworkEventType.DELTA, delta=content)
                        else:
                            yield FrameworkStreamEvent(
                                type=FrameworkEventType.STEP,
                                data={
                                    "event_type": event_type,
                                    "source": getattr(item, "source", None),
                                },
                            )
            finally:
                self._tokens.pop(request.run_id, None)
            results.append(self._result(final))

        async for event in self._stream_guarded(request, events):
            yield event


# --------------------------------------------------------------------------------------------
# CrewAI
# --------------------------------------------------------------------------------------------


class CrewAIAdapter(_BaseAdapter):
    """Runs a CrewAI ``Crew``. ``input_factory`` (default ``{"input": request.input}``) receives
    the request, so it can pass the run's identity to the crew's inputs; the adapter does not add
    keys to them. A ``pydantic`` or ``json_dict`` result is kept in ``metadata["value"]``.

    The crew runs with CrewAI's native async ``akickoff`` when it has one, which
    :meth:`cancel` stops, so ``capabilities.cancellation`` is True. Otherwise it runs with
    ``kickoff_async`` or ``kickoff``, both of which run the crew in a worker thread that keeps
    running (and can still act) after :meth:`cancel` stops waiting for it: ``cancellation`` is
    then False. ``streaming`` is False: :meth:`stream` emits STARTED and the terminal event only.
    """

    framework_id = "crewai"
    capabilities = FrameworkCapabilities(cancellation=False, multi_agent=True)

    def __init__(
        self,
        crew: Any,
        *,
        input_factory: Callable[[FrameworkRunRequest], Mapping[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self._crew = crew
        self._input_factory = input_factory or (lambda request: {"input": request.input})
        self.capabilities = FrameworkCapabilities(
            cancellation=getattr(crew, "akickoff", None) is not None, multi_agent=True
        )

    async def invoke(self, request: FrameworkRunRequest) -> FrameworkRunResult:
        async def run() -> FrameworkRunResult:
            inputs = dict(self._input_factory(request))
            native = getattr(self._crew, "akickoff", None)
            kickoff_async = getattr(self._crew, "kickoff_async", None)
            if native is not None:
                result = await self._tracked(request.run_id, native(inputs=inputs))
            elif kickoff_async is not None:
                result = await self._tracked(request.run_id, kickoff_async(inputs=inputs))
            else:
                result = await self._tracked(
                    request.run_id, asyncio.to_thread(self._crew.kickoff, inputs=inputs)
                )
            metadata: dict[str, Any] = {"framework_output_type": type(result).__name__}
            for name in ("pydantic", "json_dict"):
                value = _json_value(getattr(result, name, None))
                if value is not None:
                    metadata["value"] = value
                    break
            return FrameworkRunResult(
                output=_text(getattr(result, "raw", result)),
                usage=_usage(getattr(result, "token_usage", None)),
                metadata=metadata,
            )

        return await self._guarded(request, run)

    async def stream(self, request: FrameworkRunRequest) -> AsyncIterator[FrameworkStreamEvent]:
        yield FrameworkStreamEvent(type=FrameworkEventType.STARTED)
        yield _terminal(await self.invoke(request))
