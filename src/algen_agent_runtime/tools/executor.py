from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any

import jsonschema
import structlog
from opentelemetry import trace

from algen_agent_runtime.cache import CacheContext, CacheService
from algen_agent_runtime.exceptions.errors import PolicyDeniedError, ToolExecutionError
from algen_agent_runtime.persistence.tool_executions import InMemoryToolExecutionStore
from algen_agent_runtime.policies.instrumentation import evaluate_policy_observed
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    ReconcilableToolExecutionStore,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionRecord,
    ToolExecutionResolution,
    ToolExecutionStatus,
    ToolExecutionStore,
    ToolResult,
)
from algen_agent_runtime.tools.registry import ToolRegistry

_logger = structlog.get_logger("algen_agent_runtime.tools")

# Told about a call the tool's reconcile hook settled while it was being replayed.
ReconciledCallback = Callable[[ToolExecutionRecord], Awaitable[None]]

_COMPENSATION_STATUSES = frozenset(
    {
        ToolExecutionStatus.COMPENSATING,
        ToolExecutionStatus.COMPENSATED,
        ToolExecutionStatus.COMPENSATION_FAILED,
    }
)


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
        self._tracer = tracer or trace.get_tracer("algen_agent_runtime.tools")

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
    ) -> ToolResult:
        """Run a call, or replay its recorded outcome.

        A replayed call whose outcome is unknown (``started`` or ``indeterminate``) is first put
        to the tool's ``reconcile`` hook, when it has one and allows it on replay; a settled
        outcome is recorded and ``on_reconciled`` is told about it.
        """
        tool = self._registry.get(name)
        definition = tool.definition
        missing = definition.required_permissions - context.permissions
        if missing:
            raise PolicyDeniedError(f"missing permissions for {name}: {sorted(missing)}")
        try:
            jsonschema.validate(arguments, definition.input_schema)
        except jsonschema.ValidationError as exc:
            raise ToolExecutionError(f"invalid input for {name}: {exc.message}") from exc
        decision = await self._evaluate_policy("before_tool", arguments, definition, context, agent)
        if decision.action != "allow":
            raise PolicyDeniedError(decision.reason)
        execution, reserved = await self._executions.begin(context, name)
        if not reserved:
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
            # A call made with the caller's credentials may return what only that caller may
            # see: it is never served from, or stored in, a cache shared beyond the call.
            and not context.credentials
        )
        cache_context = CacheContext(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            run_id=context.run_id,
            authorization_fingerprint=",".join(sorted(context.permissions)),
        )
        cache_material = {
            "tool": definition.name,
            "version": definition.version,
            "arguments": arguments,
        }
        if cacheable:
            assert cache is not None
            cached = await cache.get_json(
                "tool_results", "tool.result", cache_material, cache_context
            )
            if cached is not None:
                wrapped = ToolResult.model_validate(cached).model_copy(
                    update={"metadata": {**cached.get("metadata", {}), "runtime_cache_hit": True}}
                )
                try:
                    await tool.validate_replay(wrapped, context)
                except BaseException as exc:
                    await self._executions.fail(
                        context.idempotency_key, type(exc).__name__, indeterminate=False
                    )
                    raise
                await self._executions.complete(context.idempotency_key, wrapped)
                return wrapped
        semaphore = self._semaphores.setdefault(name, asyncio.Semaphore(definition.max_concurrency))
        async with semaphore:
            attempts = (
                definition.retry_policy.max_attempts
                if definition.idempotency != Idempotency.NON_IDEMPOTENT
                else 1
            )
            result: Any = None
            try:
                for attempt in range(attempts):
                    try:
                        result = tool.handler(arguments, context)
                        if inspect.isawaitable(result):
                            result = await asyncio.wait_for(result, definition.timeout_seconds)
                        break
                    except TimeoutError as exc:
                        if attempt + 1 >= attempts:
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
                indeterminate = definition.side_effect not in {SideEffect.NONE, SideEffect.READ}
                await self._executions.fail(
                    context.idempotency_key,
                    type(exc).__name__,
                    indeterminate=indeterminate,
                )
                raise
            try:
                result = await self._checked_output(name, definition, result, context, agent)
                wrapped = ToolResult(value=result)
                await self._executions.complete(context.idempotency_key, wrapped)
                if cacheable:
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
                await self._executions.fail(
                    context.idempotency_key,
                    type(exc).__name__,
                    indeterminate=indeterminate,
                )
                raise

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
            result = ToolResult(value=value, metadata=metadata)
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
