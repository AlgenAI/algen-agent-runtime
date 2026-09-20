from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Mapping
from typing import Any, cast
from uuid import uuid4

from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.types.contracts import RequestOverrides, RunRequest, RunStatus, utc_now
from algen_agent_runtime.workflows.contracts import (
    NodeHandler,
    OutputValidator,
    PayloadBuilder,
    WorkflowAgentRunner,
    WorkflowEventSink,
    WorkflowExecutionState,
    WorkflowHookContext,
    WorkflowManifest,
    WorkflowNode,
    WorkflowNodeExecution,
    WorkflowNodeKind,
    WorkflowNodeStatus,
    WorkflowPredicate,
    WorkflowStatus,
)


class WorkflowHookRegistry:
    """Registry for application-owned domain hooks referenced by portable manifests."""

    def __init__(self) -> None:
        self._builders: dict[str, PayloadBuilder] = {}
        self._validators: dict[str, OutputValidator] = {}
        self._handlers: dict[str, NodeHandler] = {}
        self._schemas: dict[str, dict[str, Any]] = {}

    def register_builder(self, name: str, builder: PayloadBuilder) -> None:
        self._register(self._builders, name, builder)

    def register_validator(self, name: str, validator: OutputValidator) -> None:
        self._register(self._validators, name, validator)

    def register_handler(self, name: str, handler: NodeHandler) -> None:
        self._register(self._handlers, name, handler)

    def register_schema(self, name: str, schema: dict[str, Any]) -> None:
        self._register(self._schemas, name, schema)

    @staticmethod
    def _register(registry: dict[str, Any], name: str, value: Any) -> None:
        if name in registry:
            raise ConfigurationError(f"workflow hook {name!r} is already registered")
        registry[name] = value

    def builder(self, name: str) -> PayloadBuilder:
        return cast(PayloadBuilder, self._get(self._builders, name, "payload builder"))

    def validator(self, name: str) -> OutputValidator:
        return cast(OutputValidator, self._get(self._validators, name, "output validator"))

    def handler(self, name: str) -> NodeHandler:
        return cast(NodeHandler, self._get(self._handlers, name, "node handler"))

    def schema(self, name: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._get(self._schemas, name, "output schema"))

    @staticmethod
    def _get(registry: dict[str, Any], name: str, kind: str) -> Any:
        try:
            return registry[name]
        except KeyError as exc:
            raise ConfigurationError(f"workflow {kind} {name!r} is not registered") from exc


class MultiAgentWorkflowExecutor:
    """Runtime-owned DAG scheduler for typed agent, mapped-agent, and domain-handler nodes."""

    def __init__(
        self,
        runtime: Any,
        hooks: WorkflowHookRegistry,
        *,
        agent_runner: WorkflowAgentRunner | None = None,
    ) -> None:
        self._runtime = runtime
        self._hooks = hooks
        self._agent_runner = agent_runner

    async def run(
        self,
        manifest: WorkflowManifest,
        values: dict[str, Any],
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str | None = None,
        turn_id: str | None = None,
        correlation_id: str | None = None,
        emit: WorkflowEventSink | None = None,
    ) -> WorkflowExecutionState:
        state = WorkflowExecutionState(
            id=correlation_id or str(uuid4()),
            manifest_name=manifest.name,
            manifest_version=manifest.version,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            values=dict(values),
            nodes={node.id: WorkflowNodeExecution(node_id=node.id) for node in manifest.nodes},
        )
        state.status = WorkflowStatus.RUNNING
        await self._emit(emit, "workflow.started", state, None)
        try:
            async with asyncio.timeout(manifest.timeout_seconds):
                await self._schedule(manifest, state, emit)
        except asyncio.CancelledError:
            state.status = WorkflowStatus.CANCELLED
            manifest_nodes = {node.id: node for node in manifest.nodes}
            for node_id, execution in state.nodes.items():
                if execution.status in {WorkflowNodeStatus.PENDING, WorkflowNodeStatus.RUNNING}:
                    execution.status = WorkflowNodeStatus.CANCELLED
                    execution.completed_at = utc_now()
                    await self._emit(
                        emit, "workflow.node.cancelled", state, manifest_nodes[node_id]
                    )
            await self._emit(emit, "workflow.cancelled", state, None)
            raise
        except Exception as exc:
            state.status = WorkflowStatus.FAILED
            state.error = f"{type(exc).__name__}: {exc}"
            manifest_nodes = {node.id: node for node in manifest.nodes}
            for node_id, execution in state.nodes.items():
                node = manifest_nodes[node_id]
                if execution.status == WorkflowNodeStatus.RUNNING:
                    execution.status = WorkflowNodeStatus.FAILED
                    execution.error = state.error
                    execution.completed_at = utc_now()
                    await self._emit(
                        emit,
                        "workflow.node.failed",
                        state,
                        node,
                        {"error": state.error},
                    )
                elif execution.status == WorkflowNodeStatus.PENDING:
                    execution.status = WorkflowNodeStatus.SKIPPED
                    execution.error = "workflow terminated before this node could start"
                    execution.completed_at = utc_now()
                    await self._emit(emit, "workflow.node.skipped", state, node)
            await self._emit(emit, "workflow.failed", state, None, {"error": state.error})
            return state
        if state.status == WorkflowStatus.RUNNING:
            nodes = {node.id: node for node in manifest.nodes}
            failed = [
                execution
                for node_id, execution in state.nodes.items()
                if execution.status == WorkflowNodeStatus.FAILED
                and nodes[node_id].failure_policy != "continue"
            ]
            if failed:
                state.status = WorkflowStatus.FAILED
                state.error = failed[0].error
                await self._emit(emit, "workflow.failed", state, None, {"error": state.error})
            else:
                state.status = WorkflowStatus.COMPLETED
                await self._emit(emit, "workflow.completed", state, None)
        state.updated_at = utc_now()
        return state

    async def _schedule(
        self,
        manifest: WorkflowManifest,
        state: WorkflowExecutionState,
        emit: WorkflowEventSink | None,
    ) -> None:
        nodes = {node.id: node for node in manifest.nodes}
        semaphore = asyncio.Semaphore(manifest.maximum_concurrency)
        pending = set(nodes)
        while pending:
            if state.status == WorkflowStatus.AWAITING_INPUT:
                return
            propagated = True
            while propagated:
                propagated = False
                for node in manifest.nodes:
                    node_id = node.id
                    if node_id not in pending:
                        continue
                    blockers = [
                        dependency
                        for dependency in node.depends_on
                        if (
                            (
                                state.nodes[dependency].status == WorkflowNodeStatus.SKIPPED
                                and node.kind != WorkflowNodeKind.JOIN
                            )
                            or (
                                state.nodes[dependency].status == WorkflowNodeStatus.FAILED
                                and nodes[dependency].failure_policy != "continue"
                            )
                        )
                    ]
                    if blockers:
                        state.nodes[node_id].status = WorkflowNodeStatus.SKIPPED
                        state.nodes[node_id].error = f"blocked by failed dependencies: {blockers}"
                        state.nodes[node_id].completed_at = utc_now()
                        pending.remove(node_id)
                        propagated = True
                        await self._emit(emit, "workflow.node.skipped", state, node)
            if not pending:
                break
            ready = [
                node
                for node in manifest.nodes
                if node.id in pending
                if all(
                    state.nodes[dependency].status == WorkflowNodeStatus.COMPLETED
                    or (
                        node.kind == WorkflowNodeKind.JOIN
                        and state.nodes[dependency].status == WorkflowNodeStatus.SKIPPED
                    )
                    or (
                        state.nodes[dependency].status == WorkflowNodeStatus.FAILED
                        and nodes[dependency].failure_policy == "continue"
                    )
                    for dependency in node.depends_on
                )
            ]
            if not ready:
                raise RuntimeError("workflow has no dependency-ready nodes")

            async def execute(node: WorkflowNode) -> None:
                await self._execute_node(state, node, nodes, emit, semaphore)

            outcomes = await asyncio.gather(*(execute(node) for node in ready))
            del outcomes
            pending -= {node.id for node in ready}
            failed = [
                node for node in ready if state.nodes[node.id].status == WorkflowNodeStatus.FAILED
            ]
            for failed_node in failed:
                if failed_node.failure_policy == "fail_workflow":
                    raise RuntimeError(f"workflow failed at node {failed_node.id!r}")

    async def _execute_node(
        self,
        state: WorkflowExecutionState,
        node: WorkflowNode,
        nodes: Mapping[str, WorkflowNode],
        emit: WorkflowEventSink | None,
        semaphore: asyncio.Semaphore,
    ) -> None:
        execution = state.nodes[node.id]
        if node.run_if and not self._predicate(node.run_if, state.values):
            execution.status = WorkflowNodeStatus.SKIPPED
            execution.completed_at = utc_now()
            await self._emit(emit, "workflow.node.skipped", state, node)
            return
        execution.status = WorkflowNodeStatus.RUNNING
        execution.started_at = utc_now()
        await self._emit(emit, "workflow.node.started", state, node)
        try:
            if node.kind == WorkflowNodeKind.HANDLER:
                async with semaphore:
                    result = await self._call(
                        self._hooks.handler(node.handler or ""), self._context(state, node)
                    )
            elif node.kind == WorkflowNodeKind.JOIN:
                outputs = [
                    state.values[nodes[item].output_key]
                    for item in node.depends_on
                    if nodes[item].output_key in state.values
                ]
                if node.join_strategy == "json_array":
                    result = outputs
                elif node.join_strategy == "first":
                    result = outputs[0] if outputs else None
                else:
                    result = "\n\n".join(str(item) for item in outputs if item is not None)
            elif node.kind == WorkflowNodeKind.PREDICATE:
                assert node.condition is not None
                result = self._predicate(node.condition, state.values)
            elif node.kind == WorkflowNodeKind.MAP_AGENT:
                items = self._path(state.values, node.map_from or "")
                if not isinstance(items, list | tuple):
                    raise ConfigurationError(f"map source {node.map_from!r} is not a list")
                if len(items) > node.max_fan_out:
                    raise ConfigurationError(
                        f"map node {node.id!r} exceeds max_fan_out={node.max_fan_out}"
                    )
                results = await asyncio.gather(
                    *(
                        self._execute_agent(
                            state,
                            node,
                            item=item,
                            item_index=index,
                            semaphore=semaphore,
                            emit=emit,
                        )
                        for index, item in enumerate(items)
                    )
                )
                result = list(results)
            else:
                result = None
                for iteration in range(1, node.max_iterations + 1):
                    result = await self._execute_agent(
                        state,
                        node,
                        semaphore=semaphore,
                        emit=emit,
                        iteration=iteration,
                    )
                    if node.loop_until and self._predicate(
                        node.loop_until, state.values, current_output=result
                    ):
                        break
            state.values[node.output_key] = result
            execution.status = WorkflowNodeStatus.COMPLETED
            execution.completed_at = utc_now()
            if node.pause and self._predicate(node.pause.when, state.values):
                occurrences = (
                    self._path(state.values, node.pause.occurrences_from)
                    if node.pause.occurrences_from
                    else ()
                )
                occurrence_count = len(occurrences) if isinstance(occurrences, (list, tuple)) else 0
                if occurrence_count >= node.pause.maximum_occurrences:
                    raise ConfigurationError(
                        f"node {node.id!r} exceeded its clarification limit of "
                        f"{node.pause.maximum_occurrences}"
                    )
                state.pause = {
                    "node_id": node.id,
                    "question": self._path(state.values, node.pause.question_from),
                    "state": {key: self._path(state.values, key) for key in node.pause.state_from},
                }
                state.status = WorkflowStatus.AWAITING_INPUT
                await self._emit(emit, "workflow.input.required", state, node, state.pause)
            await self._emit(emit, "workflow.node.completed", state, node)
        except Exception as exc:
            execution.status = WorkflowNodeStatus.FAILED
            execution.error = f"{type(exc).__name__}: {exc}"
            execution.completed_at = utc_now()
            await self._emit(emit, "workflow.node.failed", state, node, {"error": execution.error})

    async def _execute_agent(
        self,
        state: WorkflowExecutionState,
        node: WorkflowNode,
        *,
        item: Any = None,
        item_index: int | None = None,
        semaphore: asyncio.Semaphore,
        emit: WorkflowEventSink | None,
        iteration: int = 1,
    ) -> Any:
        invalid: Any = None
        validation_error: str | None = None
        parent_run_id = self._parent_run_id(state, node)
        for attempt in range(node.max_repairs + 1):
            hook_context = self._context(
                state,
                node,
                item=item,
                item_index=item_index,
                invalid_output=invalid,
                validation_error=validation_error,
            )
            payload = (
                await self._call(self._hooks.builder(node.input_builder), hook_context)
                if node.input_builder
                else self._render_template(node.input_template or "{{input}}", state.values, item)
            )
            request_metadata: dict[str, Any] = {}
            if node.metadata_builder:
                metadata_value = await self._call(
                    self._hooks.builder(node.metadata_builder),
                    hook_context.model_copy(update={"request_payload": payload}),
                )
                if not isinstance(metadata_value, Mapping):
                    raise ConfigurationError(
                        f"metadata builder {node.metadata_builder!r} must return a mapping"
                    )
                request_metadata = dict(metadata_value)
            request = RunRequest(
                agent=node.agent or "",
                input=(
                    payload
                    if isinstance(payload, str)
                    else json.dumps(payload, indent=2, default=str)
                ),
                tenant_id=state.tenant_id,
                user_id=state.user_id,
                conversation_id=state.conversation_id,
                turn_id=state.turn_id,
                correlation_id=state.id,
                workflow_run_id=state.id,
                parent_run_id=parent_run_id,
                overrides=RequestOverrides(
                    response_schema=(
                        self._hooks.schema(node.output_schema_hook)
                        if node.output_schema_hook
                        else node.output_schema
                    )
                ),
                metadata={
                    **request_metadata,
                    "workflow_name": state.manifest_name,
                    "workflow_node_id": node.id,
                    "workflow_item_index": "" if item_index is None else str(item_index),
                    "workflow_attempt": str(attempt + 1),
                    "workflow_iteration": str(iteration),
                },
            )
            async with semaphore:
                result = (
                    await self._agent_runner(request, hook_context)
                    if self._agent_runner
                    else await self._runtime.run(request)
                )
            state.nodes[node.id].run_ids.append(result.run_id)
            parent_run_id = result.run_id
            state.nodes[node.id].attempts += 1
            if result.status != RunStatus.COMPLETED:
                raise RuntimeError(result.error or f"agent {node.agent!r} failed")
            try:
                structured = node.output_schema is not None or node.output_schema_hook is not None
                value = json.loads(result.output or "null") if structured else result.output
            except json.JSONDecodeError as exc:
                raise ConfigurationError(
                    f"agent {node.agent!r} returned invalid structured output"
                ) from exc
            if not node.validator:
                return value
            validation_error = await self._call(
                self._hooks.validator(node.validator), value, hook_context
            )
            if validation_error is None:
                return value
            invalid = value
            if attempt < node.max_repairs:
                await self._emit(
                    emit,
                    "workflow.node.repairing",
                    state,
                    node,
                    {
                        "attempt": attempt + 1,
                        "next_attempt": attempt + 2,
                        "reason": validation_error,
                    },
                )
        raise ConfigurationError(
            f"node {node.id!r} failed validation after {node.max_repairs} repair attempt(s): "
            f"{validation_error}"
        )

    @staticmethod
    def _context(
        state: WorkflowExecutionState,
        node: WorkflowNode,
        **updates: Any,
    ) -> WorkflowHookContext:
        return WorkflowHookContext(state=state, node=node, **updates)

    @staticmethod
    async def _call(function: Any, *args: Any) -> Any:
        value = function(*args)
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _path(values: Any, path: str) -> Any:
        current = values
        for part in path.split(".") if path else ():
            if isinstance(current, Mapping):
                current = current[part]
            elif isinstance(current, (list, tuple)) and part.isdigit():
                current = current[int(part)]
            else:
                current = getattr(current, part)
        return current

    @classmethod
    def _predicate(
        cls,
        predicate: WorkflowPredicate,
        values: dict[str, Any],
        *,
        current_output: Any = None,
    ) -> bool:
        try:
            actual = (
                current_output
                if predicate.source == "current.output"
                else cls._path(values, predicate.source)
            )
        except (KeyError, AttributeError, IndexError):
            actual = None
        expected = predicate.value
        if predicate.operator == "equals":
            return bool(actual == expected)
        if predicate.operator == "not_equals":
            return bool(actual != expected)
        if predicate.operator == "contains":
            return bool(expected in actual) if actual is not None else False
        if predicate.operator == "not_contains":
            return bool(expected not in actual) if actual is not None else True
        if predicate.operator == "in":
            return actual in expected if isinstance(expected, (list, tuple, set)) else False
        return bool(actual) if predicate.operator == "truthy" else not bool(actual)

    @staticmethod
    def _render_template(template: str, values: dict[str, Any], item: Any) -> str:
        rendered = template.replace("{{item}}", str(item) if item is not None else "")
        for key, value in values.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
            rendered = rendered.replace(f"{{{{{key}.output}}}}", str(value))
        return rendered

    @staticmethod
    def _parent_run_id(state: WorkflowExecutionState, node: WorkflowNode) -> str | None:
        own_run_ids = state.nodes[node.id].run_ids
        if own_run_ids:
            return own_run_ids[-1]
        for dependency in reversed(node.depends_on):
            run_ids = state.nodes[dependency].run_ids
            if run_ids:
                return run_ids[-1]
        return None

    @staticmethod
    async def _emit(
        sink: WorkflowEventSink | None,
        event_type: str,
        state: WorkflowExecutionState,
        node: WorkflowNode | None,
        data: dict[str, Any] | None = None,
    ) -> None:
        if sink is None:
            return
        payload = {
            "workflow_id": state.id,
            "workflow_name": state.manifest_name,
            "status": state.status.value,
            **({"node_id": node.id, "node_kind": node.kind.value} if node else {}),
            **(data or {}),
        }
        result = sink(event_type, payload)
        if inspect.isawaitable(result):
            await result
