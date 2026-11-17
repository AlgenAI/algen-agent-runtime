from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import jsonschema

from algen_agent_runtime.cache import CacheContext, CacheService
from algen_agent_runtime.exceptions.errors import (
    ApprovalMismatchError,
    PolicyDeniedError,
    ToolExecutionError,
    ToolNotAttemptedError,
)
from algen_agent_runtime.observability.metrics import instrument as metric_instrument
from algen_agent_runtime.observability.safe_logging import runtime_logger
from algen_agent_runtime.observability.safe_spans import runtime_tracer
from algen_agent_runtime.persistence.tool_executions import InMemoryToolExecutionStore
from algen_agent_runtime.policies.contracts import PolicyAction
from algen_agent_runtime.policies.instrumentation import evaluate_policy_observed
from algen_agent_runtime.tools.blocking import run_blocking, track_threads, when_finished
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    ReconcilableToolExecutionStore,
    SideEffect,
    Tool,
    ToolApproval,
    ToolContext,
    ToolDefinition,
    ToolExecutionRecord,
    ToolExecutionResolution,
    ToolExecutionStatus,
    ToolExecutionStore,
    ToolHandler,
    ToolResult,
    canonical_digest,
    tool_fingerprint,
)
from algen_agent_runtime.tools.process import run_in_process
from algen_agent_runtime.tools.registry import ToolRegistry

_logger = runtime_logger("algen_agent_runtime.tools")

# Told about a call the tool's reconcile hook settled while it was being replayed.
ReconciledCallback = Callable[[ToolExecutionRecord], Awaitable[None]]

_COMPENSATION_STATUSES = frozenset(
    {
        ToolExecutionStatus.COMPENSATING,
        ToolExecutionStatus.COMPENSATED,
        ToolExecutionStatus.COMPENSATION_FAILED,
    }
)


def _is_async(handler: ToolHandler) -> bool:
    # A callable object with an ``async def __call__`` is asynchronous too.
    return inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
        type(handler).__call__
    )


async def _call_handler(
    definition: ToolDefinition,
    handler: ToolHandler,
    arguments: dict[str, Any],
    context: ToolContext,
) -> Any:
    """Await an async handler; run a synchronous one in a worker thread, or any handler in a
    subprocess when the tool's ``execution`` is ``process``.

    A synchronous handler on the event loop would block every other run and outlive its
    timeout. In a thread the caller stops waiting on schedule, but the work may continue: the
    thread cannot be stopped and its side effect may still finish, which is why a timed-out call
    that changes things is recorded indeterminate, and why the call keeps its capacity until the
    thread ends. A subprocess is killed on timeout, so its work really stops.
    """
    if definition.execution == "process":
        return await run_in_process(definition.name, handler, arguments, context)
    if _is_async(handler):
        return await handler(arguments, context)
    result = await run_blocking(handler, arguments, context)
    return await result if inspect.isawaitable(result) else result


async def _landed[T](write: Awaitable[T]) -> T:
    """A ledger write awaited to the end, even when the call is cancelled meanwhile (a run
    cancelled twice, a turn and its run both cancelled): a record left ``started`` waits for an
    operator, and keeps its run from retention, for good. The cancellation is raised once the
    write has landed."""
    task = asyncio.ensure_future(write)
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            cancelled = exc
    if cancelled is not None:
        raise cancelled
    return result


def _never_started(definition: ToolDefinition, handler: ToolHandler, attempt: list[Any]) -> bool:
    """Whether a timed-out synchronous handler's call was withdrawn from the thread queue before
    it started: then nothing of it ran."""
    return (
        definition.execution == "thread"
        and not _is_async(handler)
        and bool(attempt)
        and all(thread.cancelled() for thread in attempt)
    )


def _checked_arguments(
    name: str,
    definition: ToolDefinition,
    arguments: dict[str, Any],
    decision: Any,
    *,
    approved: bool,
) -> dict[str, Any]:
    """The arguments the call runs with after the ``before_tool`` decision.

    ``allow`` keeps them. ``transform`` replaces them with the policy's value, which must still
    fit the input schema; an approved call is refused instead when the rewrite changes them, since
    its approval covers the arguments that were shown (rewrites belong at ``plan_tool``, before the
    approval is asked for). ``redact`` refuses the call: the tool would act on masked data the
    model never meant to send. Every other action refuses it too.
    """
    action = decision.action
    if action == PolicyAction.ALLOW:
        return arguments
    if action == PolicyAction.REDACT:
        raise PolicyDeniedError(
            f"{decision.reason} A redaction refuses the call to {name}; it is not run with "
            "masked arguments."
        )
    if action != PolicyAction.TRANSFORM:
        raise PolicyDeniedError(decision.reason)
    rewritten = decision.value
    if not isinstance(rewritten, dict):
        raise ToolExecutionError(f"policy-transformed input for {name} is not an object")
    try:
        jsonschema.validate(rewritten, definition.input_schema)
    except jsonschema.ValidationError as exc:
        raise ToolExecutionError(
            f"policy-transformed input for {name} is invalid: {exc.message}"
        ) from exc
    if approved and rewritten != arguments:
        raise PolicyDeniedError(
            f"a before_tool policy rewrote the arguments of an approved call to {name}; "
            "the approval does not cover them"
        )
    return rewritten


@asynccontextmanager
async def _capacity(
    semaphore: asyncio.Semaphore,
) -> AsyncIterator[list[concurrent.futures.Future[Any]]]:
    """One of the tool's ``max_concurrency`` slots, already acquired, held until the call and
    every worker thread it started have finished, even when the caller stopped waiting for a
    thread first."""
    with track_threads() as threads:
        try:
            yield threads
        finally:
            finished = when_finished(threads)
            if finished is None:
                semaphore.release()
            else:
                finished.add_done_callback(lambda _: semaphore.release())


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        policy_engine: Any,
        executions: ToolExecutionStore | None = None,
        cache: CacheService | None = None,
        tracer: Any | None = None,
    ) -> None:
        self._registry = registry
        self._policy_engine = policy_engine
        self._executions = executions or InMemoryToolExecutionStore()
        self._cache = cache
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._tracer = tracer or runtime_tracer("algen_agent_runtime.tools")
        self._call_counter = metric_instrument("algen_agent_runtime.tool.calls")
        self._call_duration = metric_instrument("algen_agent_runtime.tool.call.duration")

    @property
    def executions(self) -> ToolExecutionStore:
        """The tool-execution ledger this executor reserves and settles calls in."""
        return self._executions

    async def _evaluate_policy(
        self,
        point: str,
        payload: Any,
        definition: Any,
        context: ToolContext,
        agent: Any = None,
    ) -> Any:
        # With the agent in the context, its guardrail selection decides which policies run
        # (see GuardrailPolicy.tool_policies); without it every registered policy runs.
        policy_context: dict[str, Any] = {"tool": definition, "context": context}
        if agent is not None:
            policy_context["agent"] = agent
        return await evaluate_policy_observed(
            tracer=self._tracer,
            engine=self._policy_engine,
            point=point,
            payload=payload,
            context=policy_context,
            attributes={
                "run.id": context.run_id,
                "step.id": context.step_id,
                "tenant.id": context.tenant_id,
                "user.id": context.user_id,
                "tool.name": definition.name,
            },
        )

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
        *,
        agent: Any = None,
        on_reconciled: ReconciledCallback | None = None,
        approval: ToolApproval | None = None,
    ) -> ToolResult:
        """Run a call, or replay its recorded outcome.

        A replayed call whose outcome is unknown (``started`` or ``indeterminate``) is first put
        to the tool's ``reconcile`` hook, when it has one and allows it on replay; a settled
        outcome is recorded and ``on_reconciled`` is told about it.

        ``approval`` is the decided approval that covers this call (an approved tool call in a
        run, or an approved workflow approval node), bound to the tool and the arguments it
        approved. A tool that ``requires_approval`` is refused without one, and any call is
        refused with an ``ApprovalMismatchError`` when its approval is for another tool or
        version, other arguments, or a tool whose fingerprint has changed since. Both refusals
        come before anything is recorded or run. The executor does not look the decision up:
        its caller proves it (see "Who proves an approval is valid" in the tool docs).
        """
        started = time.monotonic()
        outcome = "error"
        try:
            result = await self._execute(
                name,
                arguments,
                context,
                agent=agent,
                on_reconciled=on_reconciled,
                approval=approval,
            )
            outcome = "failed" if result.failed else "completed"
            return result
        except (PolicyDeniedError, ApprovalMismatchError):
            outcome = "denied"
            raise
        except ToolExecutionError:
            outcome = "failed"
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            self._observe_call(name, outcome, started)

    def _observe_call(self, name: str, outcome: str, started: float) -> None:
        """Count a call in the tool metrics, by tool name and outcome only."""
        attributes = {"tool": name, "outcome": outcome}
        try:
            self._call_counter.add(1, attributes)
            self._call_duration.record(time.monotonic() - started, attributes)
        except Exception:
            _logger.debug("tool_metrics_failed", exc_info=True)

    async def _execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
        *,
        agent: Any,
        on_reconciled: ReconciledCallback | None,
        approval: ToolApproval | None,
    ) -> ToolResult:
        tool = self._registry.get(name)
        definition = tool.definition
        if approval is None:
            if definition.requires_approval:
                raise PolicyDeniedError(f"tool {name} requires an approval for this call")
        else:
            self._check_approval(tool, arguments, approval)
        missing = definition.required_permissions - context.permissions
        if missing:
            raise PolicyDeniedError(f"missing permissions for {name}: {sorted(missing)}")
        try:
            jsonschema.validate(arguments, definition.input_schema)
        except jsonschema.ValidationError as exc:
            raise ToolExecutionError(f"invalid input for {name}: {exc.message}") from exc
        decision = await self._evaluate_policy("before_tool", arguments, definition, context, agent)
        arguments = _checked_arguments(
            name, definition, arguments, decision, approved=approval is not None
        )
        begin = asyncio.ensure_future(self._executions.begin(context, name))
        try:
            execution, reserved = await _landed(begin)
        except asyncio.CancelledError:
            # Cancelled as the record was written: nothing ran.
            if not begin.cancelled() and begin.exception() is None and begin.result()[1]:
                await self._not_run(context, "CancelledError")
            raise
        # A call that changes nothing and was interrupted while running (the process died) is
        # simply run again: its ledger record is still ``started`` and takes the outcome.
        rerun = (
            not reserved
            and execution.status == ToolExecutionStatus.STARTED
            and definition.side_effect in {SideEffect.NONE, SideEffect.READ}
        )
        if not reserved and not rerun:
            if execution.status == ToolExecutionStatus.COMPLETED and execution.result:
                await tool.validate_replay(execution.result, context)
                return execution.result
            if (
                execution.status in {ToolExecutionStatus.STARTED, ToolExecutionStatus.INDETERMINATE}
                and tool.reconcile is not None
                and tool.reconcile_on_replay
            ):
                reconciled = await self._reconcile_on_replay(
                    tool, execution, context, agent, on_reconciled
                )
                if reconciled is not None:
                    return reconciled
            if execution.status in _COMPENSATION_STATUSES:
                raise ToolExecutionError(
                    f"tool {name} execution is {execution.status.value}; it does not run again"
                )
            raise ToolExecutionError(
                f"tool {name} has an unfinished {execution.status.value} execution; "
                "manual reconciliation is required"
            )
        cache = self._cache
        cacheable = (
            cache is not None
            and definition.side_effect in {SideEffect.NONE, SideEffect.READ}
            and definition.idempotency == Idempotency.IDEMPOTENT
            # A volatile tool reads state that changes on its own: a stored result would be stale
            # the moment it is served, so it is never looked up or stored.
            and not definition.volatile
            # A call made with the caller's credentials may return what only that caller may
            # see: it is never served from, or stored in, a cache shared beyond the call.
            and not context.credentials
        )
        cache_context = CacheContext(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            session_id=context.session_id,
            run_id=context.run_id,
            authorization_fingerprint=",".join(sorted(context.permissions)),
        )
        cache_material: dict[str, Any] = {
            "tool": definition.name,
            "version": definition.version,
            "arguments": arguments,
        }
        if definition.cache_context:
            # The context values the result depends on join the digested key material; a tool
            # declaring none keeps exactly the key it always had.
            cache_material["context"] = {
                field: getattr(context, field) for field in sorted(definition.cache_context)
            }
        semaphore = self._semaphores.setdefault(name, asyncio.Semaphore(definition.max_concurrency))
        try:
            # Until the handler is called nothing ran: a failure or cancellation here (the cache,
            # the wait for a slot) is a definite failure, never a record left ``started``.
            if cacheable:
                assert cache is not None
                cached = await cache.get_json(
                    "tool_results", "tool.result", cache_material, cache_context
                )
                if cached is not None:
                    wrapped = ToolResult.model_validate(cached).model_copy(
                        update={
                            "metadata": {**cached.get("metadata", {}), "runtime_cache_hit": True}
                        }
                    )
                    await tool.validate_replay(wrapped, context)
                    await _landed(self._executions.complete(context.idempotency_key, wrapped))
                    return wrapped
            await semaphore.acquire()
        except BaseException as exc:
            await self._not_run(context, type(exc).__name__)
            raise
        async with _capacity(semaphore) as threads:
            attempts = (
                definition.retry_policy.max_attempts
                if definition.idempotency != Idempotency.NON_IDEMPOTENT
                else 1
            )
            result: Any = None
            try:
                for attempt in range(attempts):
                    started = len(threads)
                    try:
                        result = await asyncio.wait_for(
                            _call_handler(definition, tool.handler, arguments, context),
                            definition.timeout_seconds,
                        )
                        break
                    except TimeoutError as exc:
                        # A timed-out thread still running would overlap a retry.
                        still_running = any(not thread.done() for thread in threads)
                        if attempt + 1 >= attempts or still_running:
                            if _never_started(definition, tool.handler, threads[started:]):
                                raise ToolNotAttemptedError(
                                    f"tool {name} timed out waiting for a worker thread; "
                                    "it did not start"
                                ) from exc
                            raise ToolExecutionError(f"tool {name} timed out") from exc
                    except (PolicyDeniedError, ToolExecutionError):
                        raise
                    except Exception as exc:
                        if attempt + 1 >= attempts:
                            raise ToolExecutionError(
                                f"tool {name} failed: {type(exc).__name__}"
                            ) from exc
                    delay = min(
                        definition.retry_policy.max_backoff_seconds,
                        definition.retry_policy.initial_backoff_seconds * (2**attempt),
                    )
                    await asyncio.sleep(delay)
            except BaseException as exc:
                # A call the tool refused before sending anything had no effect: it is a
                # definite failure, not an unknown outcome for an operator to reconcile. A
                # PolicyDeniedError is a refusal by definition; a handler raises it, or
                # ToolNotAttemptedError, only before the side effect.
                indeterminate = definition.side_effect not in {
                    SideEffect.NONE,
                    SideEffect.READ,
                } and not isinstance(exc, (ToolNotAttemptedError, PolicyDeniedError))
                await _landed(
                    self._executions.fail(
                        context.idempotency_key,
                        type(exc).__name__,
                        indeterminate=indeterminate,
                    )
                )
                raise
            try:
                # A handler may return a ToolResult to report a failure it returns as data.
                returned = result if isinstance(result, ToolResult) else ToolResult(value=result)
                failed = returned.failed or definition.reports_failure(returned.value)
                result = await self._checked_output(
                    name, definition, returned.value, context, agent
                )
                wrapped = returned.model_copy(update={"value": result, "failed": failed})
                # A returned failure finished: it is recorded completed, so a replay returns
                # it rather than running the call again, but it is never cached.
                await _landed(self._executions.complete(context.idempotency_key, wrapped))
                if cacheable and not failed:
                    assert cache is not None
                    await cache.set_json(
                        "tool_results",
                        "tool.result",
                        cache_material,
                        wrapped.model_dump(mode="json"),
                        cache_context,
                        tags=(f"tool:{definition.name}:{definition.version}",),
                    )
                return wrapped
            except BaseException as exc:
                indeterminate = definition.side_effect not in {SideEffect.NONE, SideEffect.READ}
                await _landed(
                    self._executions.fail(
                        context.idempotency_key,
                        type(exc).__name__,
                        indeterminate=indeterminate,
                    )
                )
                raise

    async def _not_run(self, context: ToolContext, error: str) -> None:
        """Record a reserved call that stopped before its handler was called: nothing ran, so it
        failed, whatever the tool's side effect."""
        await _landed(self._executions.fail(context.idempotency_key, error, indeterminate=False))

    @staticmethod
    def _check_approval(tool: Tool, arguments: dict[str, Any], approval: ToolApproval) -> None:
        """Refuse a call its approval does not cover."""
        definition = tool.definition
        if (approval.tool, approval.tool_version) != (definition.name, definition.version):
            raise ApprovalMismatchError(
                "approval.tool_mismatch",
                f"approval {approval.approval_id} covers {approval.tool} "
                f"version {approval.tool_version}, not {definition.name} "
                f"version {definition.version}",
            )
        if canonical_digest(arguments) != approval.arguments_sha256:
            raise ApprovalMismatchError(
                "approval.arguments_mismatch",
                f"the arguments of this call to {definition.name} differ from the ones "
                f"approval {approval.approval_id} approved",
            )
        if approval.tool_fingerprint is not None and approval.tool_fingerprint != (
            tool_fingerprint(tool)
        ):
            raise ApprovalMismatchError(
                "approval.tool_changed",
                f"tool {definition.name} changed after approval {approval.approval_id} "
                "was asked for; ask for it again",
            )

    async def _checked_output(
        self,
        name: str,
        definition: ToolDefinition,
        result: Any,
        context: ToolContext,
        agent: Any,
    ) -> Any:
        """A tool's output after its schema, the ``after_tool`` policies and the size limit."""
        try:
            jsonschema.validate(result, definition.output_schema)
        except jsonschema.ValidationError as exc:
            raise ToolExecutionError(f"invalid output from {name}: {exc.message}") from exc
        output_decision = await self._evaluate_policy(
            "after_tool", result, definition, context, agent
        )
        if output_decision.action not in {"allow", "redact", "transform"}:
            raise PolicyDeniedError(output_decision.reason)
        if output_decision.action in {"redact", "transform"}:
            result = output_decision.value
            try:
                jsonschema.validate(result, definition.output_schema)
            except jsonschema.ValidationError as exc:
                raise ToolExecutionError(
                    f"policy-transformed output from {name} is invalid: {exc.message}"
                ) from exc
        encoded = json.dumps(result, default=str).encode()
        if len(encoded) > definition.max_result_bytes:
            raise ToolExecutionError(f"result from {name} exceeds configured size limit")
        return result

    async def _reconcile_on_replay(
        self,
        tool: Tool,
        execution: ToolExecutionRecord,
        context: ToolContext,
        agent: Any,
        on_reconciled: ReconciledCallback | None,
    ) -> ToolResult | None:
        """The reconciled result of a replayed call, or None when its outcome stays unknown.

        A call the external system reports as failed is recorded ``failed`` and raises: the
        model, or the run, hears that it did not take effect, and it is not run again here.
        """
        store = self._executions
        if not isinstance(store, ReconcilableToolExecutionStore):
            return None
        name = tool.definition.name
        try:
            async with asyncio.timeout(tool.definition.timeout_seconds):
                outcome = await tool.reconcile_call(execution, context)
        except Exception as exc:
            _logger.warning(
                "tool_reconcile_failed",
                tool=name,
                idempotency_key=execution.idempotency_key,
                error_type=type(exc).__name__,
            )
            return None
        if outcome.status == "unknown":
            return None
        result: ToolResult | None = None
        if outcome.status == "completed":
            value = await self._checked_output(name, tool.definition, outcome.value, context, agent)
            metadata: dict[str, Any] = {"reconciled": True}
            if outcome.result_reference:
                metadata["result_reference"] = outcome.result_reference
            result = ToolResult(
                value=value,
                metadata=metadata,
                failed=tool.definition.reports_failure(outcome.value),
            )
            status = ToolExecutionStatus.COMPLETED
        else:
            status = ToolExecutionStatus.FAILED
        decision = ToolExecutionResolution(
            action="reconcile",
            previous_status=execution.status,
            status=status,
            actor_id="runtime",
            reason=outcome.detail or "",
            result_reference=outcome.result_reference,
        )
        stored = await store.transition(
            execution.model_copy(
                update={
                    "status": status,
                    "result": result,
                    "error": None if result is not None else "ReconciledFailed",
                    "resolutions": (*execution.resolutions, decision),
                }
            ),
            expected={execution.status},
        )
        if stored is None:
            # Someone else settled it first; their decision is what the next replay sees.
            return None
        if on_reconciled is not None:
            await on_reconciled(stored)
        if result is None:
            raise ToolExecutionError(f"tool {name} did not take effect (reconciled as failed)")
        return result
