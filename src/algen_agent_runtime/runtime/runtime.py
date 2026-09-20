from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

import structlog
from opentelemetry import trace

from algen_agent_runtime.approvals.service import ApprovalService, ApprovalStatus
from algen_agent_runtime.cache import CacheContext, CacheService
from algen_agent_runtime.config.registry import InMemoryAgentRegistry
from algen_agent_runtime.context.builder import ContextBuilderRegistry
from algen_agent_runtime.events.bus import InMemoryAuditLog
from algen_agent_runtime.events.contracts import AuditEvent, EventType, RunEvent
from algen_agent_runtime.exceptions.errors import (
    AlgenAgentRuntimeError,
    BudgetExceededError,
    ConflictError,
    NotFoundError,
    PolicyDeniedError,
    ProviderError,
    VerificationError,
)
from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.observability.traccia_adapter import (
    NoopObservabilityAdapter,
    ObservabilityAdapter,
)
from algen_agent_runtime.observability.trace_levels import TraceLevel, TraceLevelTracer
from algen_agent_runtime.planning.contracts import ActionType, PlannedAction
from algen_agent_runtime.planning.planners import PlannerRegistry
from algen_agent_runtime.policies.contracts import PolicyAction, PolicyDecision
from algen_agent_runtime.policies.instrumentation import evaluate_policy_observed
from algen_agent_runtime.responses.composer import ResponseComposerRegistry
from algen_agent_runtime.runtime.state_machine import validate_transition
from algen_agent_runtime.security.redaction import redact
from algen_agent_runtime.tools.contracts import ToolContext
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.types.contracts import (
    TERMINAL_STATUSES,
    AgentDefinition,
    ErrorKind,
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    RunRequest,
    RunResult,
    RunState,
    RunStatus,
    TextBlock,
    TokenUsage,
    ToolSpec,
    utc_now,
)
from algen_agent_runtime.types.interfaces import AuditLog, EventPublisher, MemoryStore, RunStore
from algen_agent_runtime.verification.verifiers import VerificationService


class AgentRuntime:
    def __init__(
        self,
        *,
        agents: InMemoryAgentRegistry,
        router: ModelRouter,
        tools: ToolRegistry,
        tool_executor: ToolExecutor,
        planners: PlannerRegistry,
        contexts: ContextBuilderRegistry,
        policies: Any,
        verifiers: VerificationService,
        composers: ResponseComposerRegistry,
        runs: RunStore,
        memory: MemoryStore,
        events: EventPublisher,
        approvals: ApprovalService,
        audits: AuditLog | None = None,
        observability: ObservabilityAdapter | None = None,
        cache: CacheService | None = None,
        telemetry_include_content: bool = False,
        telemetry_max_content_chars: int = 16_384,
        telemetry_trace_level: TraceLevel = "detailed",
    ) -> None:
        self.agents = agents
        self.router = router
        self.tools = tools
        self.tool_executor = tool_executor
        self.planners = planners
        self.contexts = contexts
        self.policies = policies
        self.verifiers = verifiers
        self.composers = composers
        self.runs = runs
        self.memory = memory
        self.events = events
        self.approvals = approvals
        self.audits = audits or InMemoryAuditLog()
        self.observability = observability or NoopObservabilityAdapter()
        self.cache = cache
        self.telemetry_include_content = telemetry_include_content
        self.telemetry_max_content_chars = telemetry_max_content_chars
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._task_lock = asyncio.Lock()
        self._accepting_runs = True
        self._shutdown_cancellation = False
        self._tracer = TraceLevelTracer(
            trace.get_tracer("algen_agent_runtime.runtime"), telemetry_trace_level
        )
        self._logger = structlog.get_logger("algen_agent_runtime.runtime")

    async def start(self, request: RunRequest) -> RunState:
        if not self._accepting_runs:
            raise ConflictError("runtime is draining and is not accepting new runs")
        agent = self.agents.get(request.agent, request.agent_version)
        timeout = request.timeout_seconds or agent.budget.max_latency_seconds
        state_values: dict[str, Any] = {
            "request": request,
            "agent_key": agent.key,
            "deadline": utc_now()
            + timedelta(seconds=min(timeout, agent.budget.max_latency_seconds)),
        }
        if request.session_id:
            state_values["session_id"] = request.session_id
        state = RunState(**state_values)
        await self.runs.create(state)
        await self._emit(state, "run.started", {"agent": agent.key})
        await self._schedule(state.id, request.tenant_id)
        return state

    async def run(self, request: RunRequest) -> RunResult:
        state = await self.start(request)
        return await self.wait(state.id, request.tenant_id)

    async def wait(self, run_id: str, tenant_id: str) -> RunResult:
        """Wait for an already-started run and compose its current result."""
        await self._require_state(run_id, tenant_id)
        async with self._task_lock:
            task = self._tasks.get(run_id)
        if task is not None:
            await task
        current = await self._require_state(run_id, tenant_id)
        agent = self.agents.get(current.request.agent, current.request.agent_version)
        composer = self.composers.get(agent.response_composer)
        return await composer.compose(current)

    async def status(self, run_id: str, tenant_id: str) -> RunState:
        return await self._require_state(run_id, tenant_id)

    async def cancel(self, run_id: str, tenant_id: str) -> RunState:
        state = await self._require_state(run_id, tenant_id)
        if state.status in TERMINAL_STATUSES:
            return state
        async with self._task_lock:
            task = self._tasks.get(run_id)
            if task and not task.done():
                task.cancel()
        if state.status not in TERMINAL_STATUSES:
            await self._transition(state, RunStatus.CANCELLED)
            await self._emit(state, "run.cancelled", {})
        return state

    async def resume(self, run_id: str, tenant_id: str, payload: Mapping[str, Any]) -> RunState:
        state = await self._require_state(run_id, tenant_id)
        if state.status == RunStatus.AWAITING_CLARIFICATION:
            clarification = str(payload.get("clarification", "")).strip()
            if not clarification:
                raise ValueError("clarification is required")
            state.messages.append(Message.text(Role.USER, clarification))
            state.pause_payload = None
            await self._transition(state, RunStatus.BUILDING_CONTEXT)
        elif state.status == RunStatus.AWAITING_APPROVAL:
            approval_id = str((state.pause_payload or {}).get("approval_id", ""))
            raw_decision = str(payload.get("decision", ""))
            try:
                decision = ApprovalStatus(raw_decision)
            except ValueError as exc:
                raise ValueError("decision must be approved, rejected, or modified") from exc
            approval = await self.approvals.decide(
                approval_id, tenant_id, decision, payload.get("modified_parameters")
            )
            call_id = str((state.pause_payload or {}).get("tool_call_id", ""))
            if approval.status == ApprovalStatus.REJECTED:
                state.error = "Proposed action was rejected."
                await self._transition(state, RunStatus.FAILED)
                await self._emit(state, "run.failed", {"reason": "approval_rejected"})
                return state
            state.approved_tool_call_ids.add(call_id)
            if approval.status == ApprovalStatus.MODIFIED:
                for index, call in enumerate(state.pending_tool_calls):
                    if call.id == call_id:
                        state.pending_tool_calls[index] = call.model_copy(
                            update={"arguments": approval.modified_parameters or {}}
                        )
            state.pause_payload = None
            await self._transition(state, RunStatus.PLANNING)
        else:
            raise ConflictError(f"run in {state.status.value!r} cannot be resumed")
        await self._schedule(run_id, tenant_id)
        return state

    async def _schedule(self, run_id: str, tenant_id: str) -> None:
        async with self._task_lock:
            existing = self._tasks.get(run_id)
            if existing and not existing.done():
                raise ConflictError("run is already executing")
            task = asyncio.create_task(self._drive(run_id, tenant_id))
            self._tasks[run_id] = task
            task.add_done_callback(self._forget_task)

    def _forget_task(self, completed: asyncio.Task[None]) -> None:
        for run_id, task in tuple(self._tasks.items()):
            if task is completed:
                self._tasks.pop(run_id, None)
                return

    async def recover(self, limit: int = 1000) -> int:
        """Resume non-paused checkpoints after a single-node process restart."""
        recovered = 0
        for state in await self.runs.list_active(limit):
            existing = self._tasks.get(state.id)
            if existing and not existing.done():
                continue
            if state.status in {
                RunStatus.AWAITING_APPROVAL,
                RunStatus.AWAITING_CLARIFICATION,
            }:
                continue
            if state.deadline and state.deadline <= utc_now():
                expected = state.version
                state.status = RunStatus.TIMED_OUT
                state.error = "Run deadline elapsed before recovery."
                state.updated_at = utc_now()
                await self.runs.save(state, expected)
                await self._emit(state, "run.failed", {"reason": "timeout_during_recovery"})
                continue
            target = self._recovery_status(state.status)
            if target != state.status:
                expected = state.version
                state.status = target
                state.updated_at = utc_now()
                await self.runs.save(state, expected)
            await self._schedule(state.id, state.request.tenant_id)
            recovered += 1
        return recovered

    async def shutdown(self, grace_seconds: float = 10.0) -> None:
        """Stop accepting runs, drain active work, then cooperatively cancel leftovers."""
        self._accepting_runs = False
        tasks = tuple(task for task in self._tasks.values() if not task.done())
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=grace_seconds)
        self._shutdown_cancellation = True
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.exception() if not task.cancelled() else None

    async def _drive(self, run_id: str, tenant_id: str) -> None:
        state = await self._require_state(run_id, tenant_id)
        agent = self.agents.get(state.request.agent, state.request.agent_version)
        with self.observability.run_scope(
            agent_id=agent.logical_id,
            agent_name=agent.name,
            tenant_id=state.request.tenant_id,
        ):
            attributes = {
                **self._span_attributes(state),
                "span.type": "agent",
                "agent.span.type": "agent",
                "gen_ai.operation.name": "invoke_agent",
            }
            span_scope = self.observability.span_scope(
                "agent.run", attributes=attributes
            ) or self._tracer.start_as_current_span(
                "agent.run",
                attributes=attributes,
            )
            with span_scope as span:
                await self._drive_observed(run_id, tenant_id, state, agent)
                current = await self._require_state(run_id, tenant_id)
                span.set_attribute("agent.run.status", current.status.value)
                span.set_attribute("agent.run.step_count", current.step_count)
                span.set_attribute("agent.run.model_calls", current.summary.model_calls)
                span.set_attribute("agent.run.tool_calls", current.summary.tool_calls)
                span.set_attribute("agent.run.retry_count", current.summary.retries)
                span.set_attribute(
                    "agent.run.outcome",
                    "completed" if current.status == RunStatus.COMPLETED else current.status.value,
                )
                span.set_attribute(
                    "agent.run.estimated_cost_usd",
                    current.summary.usage.estimated_cost_usd,
                )

    async def _drive_observed(
        self, run_id: str, tenant_id: str, state: RunState, agent: AgentDefinition
    ) -> None:
        try:
            remaining = (
                max(0.001, (state.deadline - utc_now()).total_seconds()) if state.deadline else None
            )
            async with asyncio.timeout(remaining):
                await self._execute(state, agent)
        except TimeoutError:
            current = await self._require_state(run_id, tenant_id)
            if current.status not in TERMINAL_STATUSES:
                await self._transition(current, RunStatus.TIMED_OUT)
                await self._emit(current, "run.failed", {"reason": "timeout"})
        except asyncio.CancelledError:
            current = await self._require_state(run_id, tenant_id)
            if self._shutdown_cancellation and current.status not in TERMINAL_STATUSES:
                expected = current.version
                current.status = self._recovery_status(current.status)
                current.updated_at = utc_now()
                await self.runs.save(current, expected)
            elif current.status not in TERMINAL_STATUSES:
                await self._transition(current, RunStatus.CANCELLED)
                await self._emit(current, "run.cancelled", {})
        except Exception as exc:
            current = await self._require_state(run_id, tenant_id)
            self._logger.exception(
                "agent_run_failed",
                error_type=type(exc).__name__,
                run_id=current.id,
                correlation_id=current.request.correlation_id,
                session_id=current.session_id,
                tenant_id=current.request.tenant_id,
                agent=current.agent_key,
            )
            if current.status not in TERMINAL_STATUSES:
                current.error = self._safe_error(exc)
                await self._transition(current, RunStatus.FAILED)
                await self._emit(current, "run.failed", {"error": current.error})

    async def _execute(self, state: RunState, agent: AgentDefinition) -> None:
        if state.status == RunStatus.RECEIVED:
            await self._transition(state, RunStatus.VALIDATING)
            checked_input = await self._policy_value(
                "input", state.request.input, state, {"agent": agent}
            )
            if isinstance(checked_input, str) and checked_input != state.request.input:
                state.request = state.request.model_copy(update={"input": checked_input})
            await self._transition(state, RunStatus.BUILDING_CONTEXT)
        while state.status not in TERMINAL_STATUSES and state.status not in {
            RunStatus.AWAITING_APPROVAL,
            RunStatus.AWAITING_CLARIFICATION,
        }:
            if state.step_count >= agent.max_steps:
                raise BudgetExceededError(f"maximum step limit {agent.max_steps} reached")
            self._enforce_budget(state, agent)
            if state.status == RunStatus.BUILDING_CONTEXT:
                checked_input = await self._policy_value(
                    "before_retrieval", state.request.input, state, {"agent": agent}
                )
                if isinstance(checked_input, str) and checked_input != state.request.input:
                    state.request = state.request.model_copy(update={"input": checked_input})
                builder = self.contexts.get(agent.context_builder)
                with self._tracer.start_as_current_span(
                    "agent.context.build", attributes=self._span_attributes(state)
                ) as span:
                    span.set_attribute("context.builder", agent.context_builder)
                    state.messages = list(await builder.build(state, agent))
                    span.set_attribute("context.message_count", len(state.messages))
                    span.set_attribute("retrieval.source_count", len(state.citations))
                    if state.citations:
                        span.set_attribute(
                            "retrieval.source_ids",
                            tuple(item.id for item in state.citations),
                        )
                        span.set_attribute(
                            "retrieval.maximum_score",
                            max(item.score for item in state.citations),
                        )
                checked_messages = await self._policy_value(
                    "after_retrieval", state.messages, state, {"agent": agent}
                )
                if isinstance(checked_messages, list):
                    state.messages = checked_messages
                await self._emit(
                    state,
                    "context.retrieved",
                    {
                        "message_count": len(state.messages),
                        "source_count": len(state.citations),
                        "citations": (
                            [
                                {
                                    "id": item.id,
                                    "source": item.source,
                                    "title": item.title,
                                    "uri": item.uri,
                                    "score": item.score,
                                }
                                for item in state.citations
                            ]
                            if self.telemetry_include_content
                            else []
                        ),
                        "citation_details_included": self.telemetry_include_content,
                    },
                )
                await self._transition(state, RunStatus.PLANNING)
            if state.status == RunStatus.PLANNING:
                planner = self.planners.get(agent.planning_strategy)
                with self._tracer.start_as_current_span(
                    "agent.planning", attributes=self._span_attributes(state)
                ) as planning_span:
                    planning_span.set_attribute("agent.planner.name", agent.planning_strategy)
                    plan = await planner.plan(state, agent)
                    planning_span.set_attribute("agent.plan.action_count", len(plan.actions))
                    planning_span.set_attribute(
                        "agent.plan.next_action", plan.actions[0].type.value
                    )
                    if self.telemetry_include_content:
                        planning_span.set_attribute(
                            "agent.plan.summary",
                            self._telemetry_content(plan.decision_summary),
                        )
                state.summary = state.summary.model_copy(
                    update={"decisions": (*state.summary.decisions, plan.decision_summary)}
                )
                action = plan.actions[0]
                await self._emit(state, "step.started", {"action": action.type.value}, action.id)
                with self._tracer.start_as_current_span(
                    "agent.step", attributes=self._span_attributes(state, action.id)
                ) as step_span:
                    step_span.set_attribute("agent.step.type", action.type.value)
                    if action.tool_name:
                        step_span.set_attribute("agent.step.tool_name", action.tool_name)
                    try:
                        await self._execute_action(state, agent, action)
                    except Exception as exc:
                        step_span.set_attribute("agent.step.status", "failed")
                        step_span.set_attribute("error.type", type(exc).__name__)
                        raise
                    step_span.set_attribute("agent.step.status", state.status.value)

    async def _execute_action(
        self, state: RunState, agent: AgentDefinition, action: PlannedAction
    ) -> None:
        state.step_count += 1
        if action.type == ActionType.CLARIFY:
            state.pause_payload = {"question": action.description}
            await self._transition(state, RunStatus.AWAITING_CLARIFICATION)
            await self._emit(state, "clarification.required", state.pause_payload, action.id)
        elif action.type == ActionType.TOOL:
            await self._execute_tool(state, agent, action)
        elif action.type == ActionType.MODEL:
            await self._execute_model(state, agent, action)
        elif action.type == ActionType.COMPLETE:
            await self._verify_and_complete(state, agent, action.id)
        elif action.type == ActionType.FAIL:
            state.error = action.description
            await self._transition(state, RunStatus.FAILED)
            await self._emit(state, "run.failed", {"error": state.error}, action.id)
        else:
            raise ConflictError(f"unsupported planned action {action.type.value}")

    async def _execute_model(
        self, state: RunState, agent: AgentDefinition, action: PlannedAction
    ) -> None:
        await self._transition(state, RunStatus.INVOKING_MODEL)
        tool_specs = tuple(
            ToolSpec(
                name=tool.definition.name,
                description=tool.definition.description,
                input_schema=tool.definition.input_schema,
            )
            for tool in self.tools.list()
            if tool.definition.name in agent.enabled_tools
        )
        request = ModelRequest(
            messages=tuple(state.messages),
            tools=tool_specs,
            response_schema=state.request.overrides.response_schema,
            temperature=state.request.overrides.temperature,
            max_output_tokens=min(
                state.request.overrides.max_output_tokens or agent.budget.max_output_tokens,
                max(1, agent.budget.max_tokens - state.summary.usage.total_tokens),
            ),
            timeout_seconds=max(0.001, (state.deadline - utc_now()).total_seconds())
            if state.deadline
            else None,
            stream=state.request.stream,
        )
        checked_request = await self._policy_value("before_model", request, state, {"agent": agent})
        if isinstance(checked_request, ModelRequest):
            request = checked_request
        await self._emit(state, "model.started", {}, action.id)
        profiles = (agent.default_model, *agent.fallback_models)
        with self._tracer.start_as_current_span(
            "agent.model.call", attributes=self._span_attributes(state, action.id)
        ) as span:
            if self.telemetry_include_content:
                span.set_attribute("llm.prompt", self._model_prompt(request))
                span.set_attribute(
                    "gen_ai.input.messages",
                    self._telemetry_content(
                        [message.model_dump(mode="json") for message in request.messages]
                    ),
                )
            response, cache_hit = await self._model_response(
                state, agent, request, profiles, action.id
            )
            total_tokens = response.usage.input_tokens + response.usage.output_tokens
            span.set_attribute("span.type", "LLM")
            span.set_attribute("llm.vendor", response.provider)
            span.set_attribute("llm.model", response.model)
            span.set_attribute("llm.response.id", response.id)
            span.set_attribute("llm.finish_reason", response.finish_reason.value)
            span.set_attribute("llm.latency_ms", response.latency_ms)
            span.set_attribute("llm.tool_call_count", len(response.tool_calls))
            span.set_attribute("llm.usage.prompt_tokens", response.usage.input_tokens)
            span.set_attribute("llm.usage.input_tokens", response.usage.input_tokens)
            span.set_attribute("llm.usage.completion_tokens", response.usage.output_tokens)
            span.set_attribute("llm.usage.output_tokens", response.usage.output_tokens)
            span.set_attribute("llm.usage.total_tokens", total_tokens)
            span.set_attribute("llm.usage.source", "provider_usage")
            span.set_attribute("llm.usage.prompt_source", "provider_usage")
            span.set_attribute("llm.usage.completion_source", "provider_usage")
            span.set_attribute("llm.usage.cached_tokens", response.usage.cached_tokens)
            span.set_attribute("llm.cost.usd", response.usage.estimated_cost_usd)
            span.set_attribute("llm.cost.source", "configured_provider_rates")
            span.set_attribute("gen_ai.system", response.provider)
            span.set_attribute("gen_ai.request.model", response.model)
            span.set_attribute("gen_ai.response.model", response.model)
            span.set_attribute("gen_ai.usage.input_tokens", response.usage.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", response.usage.output_tokens)
            span.set_attribute("llm.status", "completed")
            span.set_attribute("cache.hit", cache_hit)
            span.set_attribute("cache.type", "model_response")
            if self.telemetry_include_content:
                span.set_attribute(
                    "llm.completion",
                    self._telemetry_content(response.message.text_content),
                )
                span.set_attribute(
                    "gen_ai.output.message",
                    self._telemetry_content(response.message.model_dump(mode="json")),
                )
        checked_output = await self._policy_value(
            "after_model",
            response.message.text_content,
            state,
            {"agent": agent, "tool_calls": response.tool_calls},
        )
        if checked_output != response.message.text_content:
            response = response.model_copy(
                update={"message": Message.text(Role.ASSISTANT, str(checked_output))}
            )
        state.output_text = response.message.text_content or None
        state.pending_tool_calls = list(response.tool_calls)
        if response.message.text_content or response.tool_calls:
            state.messages.append(
                response.message.model_copy(update={"tool_calls": response.tool_calls})
            )
        state.summary = state.summary.model_copy(
            update={
                "model_calls": state.summary.model_calls + 1,
                "usage": self._add_usage(state.summary.usage, response.usage),
            }
        )
        await self._emit(
            state,
            "model.completed",
            {
                "provider": response.provider,
                "model": response.model,
                "finish_reason": response.finish_reason.value,
                "cache_hit": cache_hit,
                "latency_ms": response.latency_ms,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cached_tokens": response.usage.cached_tokens,
                "total_tokens": response.usage.total_tokens,
                "estimated_cost_usd": response.usage.estimated_cost_usd,
                "tool_call_count": len(response.tool_calls),
            },
            action.id,
        )
        await self._audit(
            state,
            "model.call",
            "completed",
            f"{response.provider}/{response.model}",
            {"step_id": action.id, "usage": response.usage.model_dump(mode="json")},
        )
        await self._transition(state, RunStatus.PLANNING)

    async def _model_response(
        self,
        state: RunState,
        agent: AgentDefinition,
        request: ModelRequest,
        profiles: Sequence[Any],
        step_id: str,
    ) -> tuple[ModelResponse, bool]:
        if self.cache is None or request.stream or request.raw_response_enabled:
            return await self._model_with_retry(state, agent, request, profiles, step_id), False
        material = {
            "agent": agent.key,
            "request": request.model_dump(
                mode="json", exclude={"timeout_seconds", "stream", "raw_response_enabled"}
            ),
            "profiles": [profile.model_dump(mode="json") for profile in profiles],
            "allowlist": sorted(agent.model_allowlist),
        }
        context = CacheContext(
            tenant_id=state.request.tenant_id,
            user_id=state.request.user_id,
            session_id=state.session_id,
            run_id=state.id,
            authorization_fingerprint=state.request.metadata.get("authorization_fingerprint"),
        )

        async def generate() -> dict[str, Any]:
            generated = ModelResponse.model_validate(
                await self._model_with_retry(state, agent, request, profiles, step_id)
            )
            return generated.model_dump(mode="json")

        value, hit = await self.cache.get_or_set_json(
            "model_responses",
            "model.response",
            material,
            context,
            generate,
            tags=(f"agent:{agent.key}",),
        )
        response = ModelResponse.model_validate(value)
        if hit:
            original_usage = response.usage
            response = response.model_copy(
                update={
                    "usage": TokenUsage(cached_tokens=original_usage.total_tokens),
                    "latency_ms": 0,
                    "raw_metadata": {
                        **(response.raw_metadata or {}),
                        "runtime_cache_hit": True,
                        "cached_original_usage": original_usage.model_dump(mode="json"),
                    },
                }
            )
        return response, hit

    async def _model_with_retry(
        self,
        state: RunState,
        agent: AgentDefinition,
        request: ModelRequest,
        profiles: Sequence[Any],
        step_id: str,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(agent.retry_policy.max_attempts):
            try:
                if request.stream:
                    text_parts: list[str] = []
                    completed = None
                    async for event in self.router.stream(request, profiles, agent.model_allowlist):
                        if event.delta:
                            text_parts.append(event.delta)
                            await self._emit(state, "model.delta", {"delta": event.delta}, step_id)
                        if event.response:
                            completed = event.response
                    if completed is None:
                        raise ProviderError(
                            "stream ended without a completed response", retryable=True
                        )
                    if text_parts and not completed.message.text_content:
                        completed = completed.model_copy(
                            update={"message": Message.text(Role.ASSISTANT, "".join(text_parts))}
                        )
                    try:
                        return self._validate_model_completion(completed, request)
                    except ProviderError:
                        self._record_rejected_completion(state, completed)
                        raise
                response = await self.router.generate(request, profiles, agent.model_allowlist)
                try:
                    return self._validate_model_completion(response, request)
                except ProviderError:
                    self._record_rejected_completion(state, response)
                    raise
            except ProviderError as exc:
                last_error = exc
                if not exc.retryable or attempt + 1 >= agent.retry_policy.max_attempts:
                    break
                # Rejected completions are already charged; respect deterministic run ceilings.
                self._enforce_budget(state, agent)
                if exc.error_kind == ErrorKind.INVALID_RESPONSE and "output-token limit" in str(
                    exc
                ):
                    current_limit = request.max_output_tokens or agent.budget.max_output_tokens
                    remaining_tokens = max(
                        1, agent.budget.max_tokens - state.summary.usage.total_tokens
                    )
                    next_limit = min(max(current_limit + 256, current_limit * 2), remaining_tokens)
                    if next_limit <= current_limit:
                        break
                    request = request.model_copy(update={"max_output_tokens": next_limit})
                    await self._audit(
                        state,
                        "model.output_limit_recovery",
                        "retrying",
                        f"{profiles[0].provider}/{profiles[0].model}",
                        {
                            "step_id": step_id,
                            "previous_output_limit": current_limit,
                            "next_output_limit": next_limit,
                        },
                    )
                state.summary = state.summary.model_copy(
                    update={"retries": state.summary.retries + 1}
                )
                state.attempt_count += 1
                await self._transition(state, RunStatus.RETRYING)
                delay = min(
                    agent.retry_policy.max_backoff_seconds,
                    agent.retry_policy.initial_backoff_seconds * (2**attempt),
                )
                await self._emit(
                    state,
                    "model.retrying",
                    {
                        "attempt": attempt + 1,
                        "next_attempt": attempt + 2,
                        "max_attempts": agent.retry_policy.max_attempts,
                        "error_kind": exc.error_kind.value,
                        "delay_seconds": delay,
                        "provider": profiles[0].provider,
                        "model": profiles[0].model,
                    },
                    step_id,
                )
                await asyncio.sleep(delay)
                await self._transition(state, RunStatus.INVOKING_MODEL)
        assert last_error is not None
        raise last_error

    @staticmethod
    def _validate_model_completion(response: Any, request: ModelRequest) -> Any:
        if response.finish_reason == FinishReason.LENGTH:
            raise ProviderError(
                "model reached its output-token limit before completing the response",
                ErrorKind.INVALID_RESPONSE,
                retryable=True,
            )
        if response.finish_reason == FinishReason.CONTENT_FILTER:
            raise ProviderError(
                "model response was blocked by the provider content filter",
                ErrorKind.INVALID_RESPONSE,
                retryable=False,
            )
        if request.response_schema and not response.message.text_content.strip():
            raise ProviderError(
                "model returned an empty schema-constrained response",
                ErrorKind.INVALID_RESPONSE,
                retryable=True,
            )
        return response

    def _record_rejected_completion(self, state: RunState, response: Any) -> None:
        """Charge truncated/empty provider completions before attempting recovery."""
        state.summary = state.summary.model_copy(
            update={
                "model_calls": state.summary.model_calls + 1,
                "usage": self._add_usage(state.summary.usage, response.usage),
            }
        )

    @staticmethod
    def _enforce_budget(state: RunState, agent: AgentDefinition) -> None:
        """Enforce local termination ceilings; platform governance remains external."""
        usage = state.summary.usage
        if usage.total_tokens >= agent.budget.max_tokens:
            raise BudgetExceededError("token budget exhausted")
        if usage.estimated_cost_usd >= agent.budget.max_cost_usd:
            raise BudgetExceededError("cost budget exhausted")

    async def _execute_tool(
        self, state: RunState, agent: AgentDefinition, action: PlannedAction
    ) -> None:
        if action.tool_name not in agent.enabled_tools:
            raise PolicyDeniedError(f"tool {action.tool_name!r} is not enabled for this agent")
        tool = self.tools.get(action.tool_name or "")
        call_id = action.id.removeprefix("tool-")
        decision = await self._evaluate_policy(
            "plan_tool",
            action.arguments,
            state,
            {"tool": tool.definition, "approval_policy": agent.approval_policy},
        )
        if (
            decision.action == PolicyAction.REQUIRE_APPROVAL
            and call_id not in state.approved_tool_call_ids
        ):
            approval = await self.approvals.create(
                state.id,
                action.id,
                state.request.tenant_id,
                f"Execute {tool.definition.name}",
                self._redact_mapping(action.arguments),
                decision.audit_metadata.get("risk", tool.definition.side_effect.value),
                agent.approval_policy.expires_seconds,
            )
            state.pause_payload = {
                "approval_id": approval.id,
                "tool_call_id": call_id,
                "proposed_action": approval.proposed_action,
                "side_effect_summary": approval.side_effect_summary,
                "redacted_parameters": approval.redacted_parameters,
                "risk": approval.risk,
                "expires_at": approval.expires_at.isoformat(),
                "options": ["approved", "rejected", "modified"],
            }
            await self._transition(state, RunStatus.AWAITING_APPROVAL)
            await self._emit(state, "approval.required", state.pause_payload, action.id)
            return
        await self._transition(state, RunStatus.INVOKING_TOOL)
        await self._emit(
            state,
            "tool.started",
            {
                "tool": tool.definition.name,
                "status": "running",
                "side_effect": tool.definition.side_effect.value,
            },
            action.id,
        )
        context = ToolContext(
            run_id=state.id,
            step_id=action.id,
            tenant_id=state.request.tenant_id,
            user_id=state.request.user_id,
            permissions=agent.tool_permissions,
            idempotency_key=f"{state.id}:{call_id}",
        )
        with self._tracer.start_as_current_span(
            "agent.tool.call", attributes=self._span_attributes(state, action.id)
        ) as span:
            span.set_attribute("span.type", "tool")
            span.set_attribute("tool.name", tool.definition.name)
            span.set_attribute("tool.version", tool.definition.version)
            span.set_attribute("tool.side_effect", tool.definition.side_effect.value)
            span.set_attribute("tool.idempotency", tool.definition.idempotency.value)
            span.set_attribute("tool.declared_cost_usd", tool.definition.cost_usd)
            span.set_attribute("agent.tool.name", tool.definition.name)
            span.set_attribute("agent.tool.side_effect", tool.definition.side_effect.value)
            if self.telemetry_include_content:
                span.set_attribute("tool.input", self._telemetry_content(action.arguments))
            try:
                result = await self.tool_executor.execute(
                    tool.definition.name, action.arguments, context
                )
            except Exception as exc:
                span.set_attribute("tool.status", "failed")
                span.set_attribute("error.type", type(exc).__name__)
                raise
            span.set_attribute("tool.status", "completed")
            span.set_attribute("tool.result.redacted", result.redacted)
            span.set_attribute("tool.artifact_count", len(result.artifact_ids))
            if self.telemetry_include_content:
                span.set_attribute("tool.output", self._telemetry_content(result.value))
        state.completed_tool_call_ids.add(call_id)
        state.pending_tool_calls = [call for call in state.pending_tool_calls if call.id != call_id]
        state.messages.append(
            Message(
                role=Role.TOOL,
                name=tool.definition.name,
                tool_call_id=call_id,
                content=(TextBlock(text=json.dumps(result.value, default=str)),),
            )
        )
        state.output_text = None
        state.summary = state.summary.model_copy(
            update={"tool_calls": state.summary.tool_calls + 1}
        )
        await self._emit(
            state,
            "tool.completed",
            {
                "tool": tool.definition.name,
                "status": "completed",
                "side_effect": tool.definition.side_effect.value,
                "result_redacted": result.redacted,
                "artifact_count": len(result.artifact_ids),
                "artifact_ids": list(result.artifact_ids) if self.telemetry_include_content else [],
            },
            action.id,
        )
        await self._audit(
            state,
            "tool.call",
            "completed",
            tool.definition.name,
            {"step_id": action.id, "side_effect": tool.definition.side_effect.value},
        )
        await self._transition(state, RunStatus.BUILDING_CONTEXT)

    async def _verify_and_complete(
        self, state: RunState, agent: AgentDefinition, step_id: str
    ) -> None:
        await self._transition(state, RunStatus.VERIFYING)
        with self._tracer.start_as_current_span(
            "agent.verification", attributes=self._span_attributes(state, step_id)
        ) as span:
            verifier_names = list(agent.verification_policy.verifiers)
            if state.request.overrides.response_schema and "json_schema" not in verifier_names:
                verifier_names.append("json_schema")
            if agent.guardrail_policy.require_citations and "citations" not in verifier_names:
                verifier_names.append("citations")
            results = await self.verifiers.verify(
                verifier_names,
                state.output_text,
                {
                    "state": state,
                    "agent": agent,
                    "schema": state.request.overrides.response_schema,
                },
            )
            passed = all(
                item.passed and item.confidence >= agent.verification_policy.minimum_confidence
                for item in results
            )
            span.set_attribute("verification.passed", passed)
            span.set_attribute("verification.validator_count", len(results))
            span.set_attribute(
                "verification.minimum_confidence",
                agent.verification_policy.minimum_confidence,
            )
            if results:
                span.set_attribute(
                    "verification.confidence",
                    min(item.confidence for item in results),
                )
        await self._emit(
            state,
            "verification.completed",
            {
                "passed": passed,
                "checks": [
                    {
                        "passed": item.passed,
                        "confidence": item.confidence,
                        "reason_code": item.reason_code,
                    }
                    for item in results
                ],
                "repair_attempt": state.attempt_count,
            },
            step_id,
        )
        if not passed:
            if state.attempt_count < agent.verification_policy.max_repairs:
                state.attempt_count += 1
                state.output_text = None
                state.messages.append(
                    Message.text(Role.SYSTEM, "Repair the previous response to satisfy validation.")
                )
                state.summary = state.summary.model_copy(
                    update={"retries": state.summary.retries + 1}
                )
                await self._transition(state, RunStatus.RETRYING)
                await self._transition(state, RunStatus.PLANNING)
                return
            raise VerificationError("final output failed verification")
        state.output_text = str(
            await self._policy_value(
                "final_response", state.output_text or "", state, {"agent": agent}
            )
        )
        await self._transition(state, RunStatus.COMPOSING)
        if agent.memory_policy.enabled:
            persisted = [Message.text(Role.USER, state.request.input)]
            if state.output_text:
                persisted.append(Message.text(Role.ASSISTANT, state.output_text))
            checked_memory = await self._policy_value(
                "before_memory_write", persisted, state, {"agent": agent}
            )
            if isinstance(checked_memory, list) and all(
                isinstance(message, Message) for message in checked_memory
            ):
                persisted = checked_memory
            with self._tracer.start_as_current_span(
                "agent.memory.write", attributes=self._span_attributes(state)
            ) as span:
                span.set_attribute("memory.operation", "append")
                span.set_attribute("memory.item_count", len(persisted))
                span.set_attribute(
                    "memory.retention_seconds", agent.memory_policy.retention_seconds
                )
                await self.memory.append(state.request.tenant_id, state.session_id, persisted)
            await self._emit(
                state,
                "memory.written",
                {
                    "operation": "append",
                    "item_count": len(persisted),
                    "retention_seconds": agent.memory_policy.retention_seconds,
                },
                step_id,
            )
            await self._policy_value(
                "after_memory_write", {"items": len(persisted)}, state, {"agent": agent}
            )
        await self._transition(state, RunStatus.COMPLETED)
        await self._emit(state, "run.completed", {"outcome": "completed"}, step_id)

    async def _transition(self, state: RunState, target: RunStatus) -> None:
        validate_transition(state.status, target)
        expected = state.version
        state.status = target
        state.updated_at = utc_now()
        await self.runs.save(state, expected)

    async def _emit(
        self,
        state: RunState,
        event_type: EventType,
        data: dict[str, Any],
        step_id: str | None = None,
    ) -> None:
        sequence = await self.events.next_sequence(state.id)
        span_context = trace.get_current_span().get_span_context()
        await self.events.publish(
            RunEvent(
                type=event_type,
                run_id=state.id,
                tenant_id=state.request.tenant_id,
                session_id=state.session_id,
                correlation_id=state.request.correlation_id,
                conversation_id=state.request.conversation_id,
                turn_id=state.request.turn_id,
                parent_run_id=state.request.parent_run_id,
                workflow_run_id=state.request.workflow_run_id,
                step_id=step_id,
                trace_id=(f"{span_context.trace_id:032x}" if span_context.is_valid else None),
                span_id=(f"{span_context.span_id:016x}" if span_context.is_valid else None),
                sequence=sequence,
                data=data,
            )
        )

    async def _require_state(self, run_id: str, tenant_id: str) -> RunState:
        state = await self.runs.get(run_id, tenant_id)
        if state is None:
            raise NotFoundError(f"run {run_id!r} not found")
        return state

    async def _policy_value(
        self,
        point: str,
        payload: Any,
        state: RunState,
        context: Mapping[str, Any],
    ) -> Any:
        decision = await self._evaluate_policy(
            point,
            payload,
            state,
            context,
        )
        await self._audit(
            state,
            "policy.decision",
            decision.action.value,
            point,
            {
                "reason_code": decision.reason_code,
                "audit_metadata": decision.audit_metadata,
            },
        )
        if decision.action == PolicyAction.DENY:
            raise PolicyDeniedError(f"{decision.reason_code}: {decision.reason}")
        if decision.action in {PolicyAction.REDACT, PolicyAction.TRANSFORM}:
            return decision.value
        if decision.action in {
            PolicyAction.REQUIRE_APPROVAL,
            PolicyAction.REQUIRE_CLARIFICATION,
        }:
            raise PolicyDeniedError(
                f"policy action {decision.action.value} is unsupported at boundary {point}"
            )
        return payload

    async def _evaluate_policy(
        self,
        point: str,
        payload: Any,
        state: RunState,
        context: Mapping[str, Any],
    ) -> PolicyDecision:
        decision = await evaluate_policy_observed(
            tracer=self._tracer,
            engine=self.policies,
            point=point,
            payload=payload,
            context={
                **context,
                "run_id": state.id,
                "tenant_id": state.request.tenant_id,
                "user_id": state.request.user_id,
                "usage": state.summary.usage,
                "step_count": state.step_count,
            },
            attributes=self._span_attributes(state),
        )
        invoked = tuple(decision.audit_metadata.get("policies_invoked", ()))
        triggered = tuple(decision.audit_metadata.get("policies_triggered", ()))
        skipped = tuple(decision.audit_metadata.get("policies_skipped", ()))
        await self._emit(
            state,
            "policy.evaluated",
            {
                "boundary": str(point),
                "action": decision.action.value,
                "reason_code": decision.reason_code,
                "invoked_count": len(invoked),
                "triggered_count": len(triggered),
                "skipped_count": len(skipped),
            },
        )
        return decision

    async def _audit(
        self,
        state: RunState,
        action: str,
        outcome: str,
        resource_id: str,
        metadata: dict[str, Any],
    ) -> None:
        await self.audits.append(
            AuditEvent(
                action=action,
                outcome=outcome,
                tenant_id=state.request.tenant_id,
                actor_id=state.request.user_id,
                resource_id=resource_id,
                metadata=redact(metadata),
            )
        )

    def _span_attributes(self, state: RunState, step_id: str | None = None) -> dict[str, str]:
        agent = self.agents.get(state.request.agent, state.request.agent_version)
        agent_id = agent.logical_id
        agent_name = agent.name
        attributes = {
            "agent.id": agent_id,
            "agent.name": agent_name,
            "agent.definition.id": agent.key,
            "agent.run.id": state.id,
            "agent.session.id": state.session_id,
            "agent.tenant.id": state.request.tenant_id,
            "agent.user.id": state.request.user_id,
            "agent.correlation.id": state.request.correlation_id,
            "session.id": state.session_id,
            "tenant.id": state.request.tenant_id,
            "user.id": state.request.user_id,
            "gen_ai.agent.id": agent_id,
            "gen_ai.agent.name": agent_name,
            "gen_ai.agent.version": agent.version,
            "gen_ai.conversation.id": state.session_id,
            "agent.type": agent.metadata.get("type", "workflow" if agent.workflow else "agent"),
            "agent.version": agent.version,
            "agent.description": agent.description,
            "agent.capabilities": ",".join(sorted(agent.capabilities)),
            "agent.tags": ",".join(sorted(agent.tags)),
            "agent.planning.strategy": agent.planning_strategy,
            "agent.run.mode": "streaming" if state.request.stream else "standard",
        }
        optional_attributes = {
            "agent.conversation.id": state.request.conversation_id,
            "agent.turn.id": state.request.turn_id,
            "agent.parent_run.id": state.request.parent_run_id,
            "agent.workflow_run.id": state.request.workflow_run_id,
        }
        attributes.update(
            {key: value for key, value in optional_attributes.items() if value is not None}
        )
        attributes.update(self.observability.span_attributes())
        if step_id:
            attributes["agent.step.id"] = step_id
        return attributes

    def _model_prompt(self, request: ModelRequest) -> str:
        prompt = "\n".join(
            f"{message.role.value}: {message.text_content}" for message in request.messages
        )
        return self._telemetry_content(prompt)

    def _telemetry_content(self, value: Any) -> str:
        safe = redact(value)
        rendered = safe if isinstance(safe, str) else json.dumps(safe, default=str)
        if len(rendered) <= self.telemetry_max_content_chars:
            return rendered
        return rendered[: self.telemetry_max_content_chars] + "...[TRUNCATED]"

    @staticmethod
    def _add_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=left.input_tokens + right.input_tokens,
            output_tokens=left.output_tokens + right.output_tokens,
            cached_tokens=left.cached_tokens + right.cached_tokens,
            estimated_cost_usd=left.estimated_cost_usd + right.estimated_cost_usd,
        )

    @staticmethod
    def _redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
        sensitive = {"password", "secret", "token", "api_key", "authorization"}
        return {
            key: "[REDACTED]" if key.lower() in sensitive else item for key, item in value.items()
        }

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(exc, AlgenAgentRuntimeError):
            return str(exc)
        if isinstance(exc, ImportError) and re.fullmatch(
            r"install algen-agent-runtime\[[a-z0-9-]+\](?: for [A-Za-z0-9 ._-]+)?",
            str(exc),
        ):
            # Runtime-authored dependency guidance is safe to expose. Arbitrary import errors
            # remain hidden because they can disclose local module names and filesystem details.
            return str(exc)
        return f"Internal error ({type(exc).__name__})"

    @staticmethod
    def _recovery_status(status: RunStatus) -> RunStatus:
        if status == RunStatus.VALIDATING:
            return RunStatus.RECEIVED
        if status in {
            RunStatus.INVOKING_MODEL,
            RunStatus.INVOKING_TOOL,
            RunStatus.VERIFYING,
            RunStatus.RETRYING,
            RunStatus.COMPOSING,
        }:
            return RunStatus.PLANNING
        return status
