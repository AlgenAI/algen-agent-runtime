from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from jsonschema import Draft202012Validator

from algen_agent_runtime.exceptions.errors import ConfigurationError, ConflictError, NotFoundError
from algen_agent_runtime.types.contracts import RequestOverrides, RunRequest, RunStatus, utc_now
from algen_agent_runtime.workflows.contracts import (
    NodeHandler,
    OutputValidator,
    PayloadBuilder,
    WorkflowAgentRunner,
    WorkflowApprovalDecision,
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
from algen_agent_runtime.workflows.store import (
    InMemoryWorkflowCheckpointStore,
    WorkflowCheckpointStore,
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


class WorkflowRegistry:
    """Version-pinned workflow and hook registry used for child dispatch."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], tuple[WorkflowManifest, WorkflowHookRegistry]] = {}

    def register(self, manifest: WorkflowManifest, hooks: WorkflowHookRegistry) -> None:
        key = (manifest.name, manifest.version)
        if key in self._entries:
            raise ConfigurationError(
                f"workflow {manifest.name!r} version {manifest.version!r} is already registered"
            )
        self._entries[key] = (manifest, hooks)

    def resolve(self, name: str, version: str) -> tuple[WorkflowManifest, WorkflowHookRegistry]:
        try:
            return self._entries[(name, version)]
        except KeyError as exc:
            raise ConfigurationError(
                f"child workflow {name!r} version {version!r} is not registered"
            ) from exc

    def list(self) -> tuple[WorkflowManifest, ...]:
        return tuple(self._entries[key][0] for key in sorted(self._entries))


class MultiAgentWorkflowExecutor:
    """Runtime-owned DAG scheduler for typed agent, mapped-agent, and domain-handler nodes."""

    def __init__(
        self,
        runtime: Any,
        hooks: WorkflowHookRegistry,
        *,
        agent_runner: WorkflowAgentRunner | None = None,
        store: WorkflowCheckpointStore | None = None,
        workflow_registry: WorkflowRegistry | None = None,
        maximum_workflow_depth: int = 8,
    ) -> None:
        self._runtime = runtime
        self._hooks = hooks
        self._agent_runner = agent_runner
        self._store = store or InMemoryWorkflowCheckpointStore()
        self._workflow_registry = workflow_registry
        if not 1 <= maximum_workflow_depth <= 32:
            raise ValueError("maximum_workflow_depth must be between 1 and 32")
        self._maximum_workflow_depth = maximum_workflow_depth
        self._checkpoint_locks: dict[str, asyncio.Lock] = {}

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
        workflow_id: str | None = None,
        parent_workflow_id: str | None = None,
        parent_workflow_node_id: str | None = None,
        root_workflow_id: str | None = None,
        workflow_depth: int = 0,
        workflow_ancestry: tuple[str, ...] = (),
        emit: WorkflowEventSink | None = None,
    ) -> WorkflowExecutionState:
        if workflow_depth > self._maximum_workflow_depth:
            raise ConfigurationError(
                f"workflow nesting exceeds maximum depth {self._maximum_workflow_depth}"
            )
        state_id = workflow_id or correlation_id or str(uuid4())
        state = WorkflowExecutionState(
            id=state_id,
            manifest_name=manifest.name,
            manifest_version=manifest.version,
            manifest_fingerprint=self._manifest_fingerprint(manifest),
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            correlation_id=correlation_id or state_id,
            parent_workflow_id=parent_workflow_id,
            parent_workflow_node_id=parent_workflow_node_id,
            root_workflow_id=root_workflow_id or state_id,
            workflow_depth=workflow_depth,
            workflow_ancestry=workflow_ancestry or (f"{manifest.name}@{manifest.version}",),
            deadline_at=utc_now() + timedelta(seconds=manifest.timeout_seconds),
            values=dict(values),
            nodes={node.id: WorkflowNodeExecution(node_id=node.id) for node in manifest.nodes},
        )
        await self._store.create(state)
        state.status = WorkflowStatus.RUNNING
        await self._emit(emit, "workflow.started", state, None)
        return await self._drive(manifest, state, emit, validate_input=True)

    async def resume(
        self,
        manifest: WorkflowManifest,
        workflow_id: str,
        *,
        tenant_id: str,
        values: Mapping[str, Any] | None = None,
        emit: WorkflowEventSink | None = None,
    ) -> WorkflowExecutionState:
        state = await self._require_state(workflow_id, tenant_id)
        self._validate_manifest_identity(manifest, state)
        if state.status is not WorkflowStatus.AWAITING_INPUT or state.pause is None:
            raise ConflictError(f"workflow {workflow_id!r} is not awaiting input")
        node_id = str(state.pause.get("node_id") or "")
        nodes = {node.id: node for node in manifest.nodes}
        if node_id not in nodes:
            raise ConfigurationError("workflow checkpoint pause references an unknown node")
        if state.pause.get("kind") == "child_workflow":
            return await self._resume_child_workflow(
                manifest,
                state,
                nodes[node_id],
                values=values,
                emit=emit,
            )
        if values:
            state.values.update(dict(values))
        self._extend_deadline_for_pause(state)
        execution = state.nodes[node_id]
        execution.status = WorkflowNodeStatus.PENDING
        execution.error = None
        execution.started_at = None
        execution.completed_at = None
        state.values.pop(nodes[node_id].output_key, None)
        state.pause = None
        state.status = WorkflowStatus.RUNNING
        await self._emit(emit, "workflow.resumed", state, nodes[node_id])
        return await self._drive(manifest, state, emit, validate_input=False)

    async def decide_approval(
        self,
        manifest: WorkflowManifest,
        workflow_id: str,
        *,
        tenant_id: str,
        user_id: str,
        decision: str,
        modified_parameters: Mapping[str, Any] | None = None,
        comment: str | None = None,
        emit: WorkflowEventSink | None = None,
    ) -> WorkflowExecutionState:
        state = await self._require_state(workflow_id, tenant_id)
        self._validate_manifest_identity(manifest, state)
        if state.status is not WorkflowStatus.AWAITING_APPROVAL or state.pause is None:
            raise ConflictError(f"workflow {workflow_id!r} is not awaiting approval")
        if state.pause.get("kind") == "child_workflow":
            node_id = str(state.pause.get("node_id") or "")
            nodes = {node.id: node for node in manifest.nodes}
            node = nodes.get(node_id)
            if node is None:
                raise ConfigurationError("workflow checkpoint pause references an unknown node")
            return await self._resume_child_workflow(
                manifest,
                state,
                node,
                decision=decision,
                modified_parameters=modified_parameters,
                comment=comment,
                approval_user_id=user_id,
                emit=emit,
            )
        if state.pause.get("kind") != "approval":
            raise ConflictError("workflow pause is not an approval checkpoint")
        node_id = str(state.pause.get("node_id") or "")
        nodes = {node.id: node for node in manifest.nodes}
        node = nodes.get(node_id)
        if node is None or node.kind is not WorkflowNodeKind.APPROVAL or node.approval is None:
            raise ConfigurationError("workflow checkpoint approval references an invalid node")
        if self._approval_is_expired(state):
            await self._expire_approval(state, node, emit)
            return await self._drive(manifest, state, emit, validate_input=False)
        if decision not in {"approved", "modified", "rejected"}:
            raise ValueError("decision must be approved, modified, or rejected")
        if not user_id.strip():
            raise ValueError("approval user_id is required")
        parameters: dict[str, Any] | None
        if decision == "modified":
            if not node.approval.allow_modification:
                raise ConflictError(f"approval node {node.id!r} does not permit modification")
            if modified_parameters is None:
                raise ValueError("modified approval requires modified_parameters")
            parameters = dict(modified_parameters)
        else:
            parameters = self._approval_parameters(node, state)
        if node.approval.parameters_schema is not None and parameters is not None:
            self._validate_contract(
                node.approval.parameters_schema,
                parameters,
                contract=f"approval parameters for {node.id!r}",
            )
        result = WorkflowApprovalDecision(
            decision=cast(Any, decision),
            parameters=parameters,
            decided_by=user_id,
            comment=comment,
        )
        # Invalid or unauthorized decision attempts must not extend the execution
        # deadline. Account for the human wait only after the decision contract
        # has been accepted.
        self._extend_deadline_for_pause(state)
        state.values[node.output_key] = result.model_dump(mode="json")
        execution = state.nodes[node.id]
        execution.status = (
            WorkflowNodeStatus.REJECTED if decision == "rejected" else WorkflowNodeStatus.COMPLETED
        )
        execution.completed_at = utc_now()
        execution.error = None
        state.pause = None
        state.status = WorkflowStatus.RUNNING
        await self._emit(
            emit,
            "workflow.approval.decided",
            state,
            node,
            {"decision": decision, "decided_by": user_id},
        )
        await self._emit(
            emit,
            "workflow.node.rejected" if decision == "rejected" else "workflow.node.completed",
            state,
            node,
            {"decision": decision},
        )
        return await self._drive(manifest, state, emit, validate_input=False)

    async def _resume_child_workflow(
        self,
        manifest: WorkflowManifest,
        state: WorkflowExecutionState,
        node: WorkflowNode,
        *,
        values: Mapping[str, Any] | None = None,
        decision: str | None = None,
        modified_parameters: Mapping[str, Any] | None = None,
        comment: str | None = None,
        approval_user_id: str | None = None,
        emit: WorkflowEventSink | None = None,
    ) -> WorkflowExecutionState:
        if self._workflow_registry is None or node.kind is not WorkflowNodeKind.WORKFLOW:
            raise ConfigurationError("child workflow resume requires a registered workflow node")
        pause = state.pause or {}
        child_id = str(pause.get("child_workflow_id") or "")
        child_name = str(pause.get("child_workflow_name") or "")
        child_version = str(pause.get("child_workflow_version") or "")
        if not child_id or (child_name, child_version) != (
            node.workflow_name,
            node.workflow_version,
        ):
            raise ConfigurationError("child workflow pause does not match its dispatch node")
        child_manifest, child_hooks = self._workflow_registry.resolve(child_name, child_version)

        async def child_event(event_type: str, data: dict[str, Any]) -> None:
            await self._emit(
                emit,
                "workflow.child.event",
                state,
                node,
                {
                    "child_event_type": event_type,
                    "child_workflow_id": child_id,
                    **(
                        {"child_node_id": str(data["node_id"])}
                        if data.get("node_id") is not None
                        else {}
                    ),
                },
            )

        child_executor = MultiAgentWorkflowExecutor(
            self._runtime,
            child_hooks,
            agent_runner=self._agent_runner,
            store=self._store,
            workflow_registry=self._workflow_registry,
            maximum_workflow_depth=self._maximum_workflow_depth,
        )
        if decision is None:
            child = await child_executor.resume(
                child_manifest,
                child_id,
                tenant_id=state.tenant_id,
                values=values,
                emit=child_event,
            )
        else:
            child = await child_executor.decide_approval(
                child_manifest,
                child_id,
                tenant_id=state.tenant_id,
                user_id=approval_user_id or state.user_id,
                decision=decision,
                modified_parameters=modified_parameters,
                comment=comment,
                emit=child_event,
            )
        self._extend_deadline_for_pause(state)
        state.pause = None
        state.status = WorkflowStatus.RUNNING
        try:
            paused, result = await self._consume_child_state(
                state, node, child_manifest, child, emit
            )
        except Exception as exc:
            execution = state.nodes[node.id]
            execution.status = WorkflowNodeStatus.FAILED
            execution.error = f"{type(exc).__name__}: {exc}"
            execution.completed_at = utc_now()
            await self._emit(
                emit,
                "workflow.node.failed",
                state,
                node,
                {"error": execution.error},
            )
            return await self._drive(manifest, state, emit, validate_input=False)
        if paused:
            return state
        state.values[node.output_key] = result
        execution = state.nodes[node.id]
        execution.status = WorkflowNodeStatus.COMPLETED
        execution.error = None
        execution.completed_at = utc_now()
        await self._emit(emit, "workflow.node.completed", state, node)
        return await self._drive(manifest, state, emit, validate_input=False)

    async def recover(
        self,
        manifest: WorkflowManifest,
        workflow_id: str,
        *,
        tenant_id: str,
        emit: WorkflowEventSink | None = None,
    ) -> WorkflowExecutionState:
        state = await self._require_state(workflow_id, tenant_id)
        self._validate_manifest_identity(manifest, state)
        nodes = {node.id: node for node in manifest.nodes}
        node_id = str((state.pause or {}).get("node_id") or "")
        node = nodes.get(node_id)
        if (
            state.status in {WorkflowStatus.AWAITING_INPUT, WorkflowStatus.AWAITING_APPROVAL}
            and (state.pause or {}).get("kind") == "child_workflow"
        ):
            if node is None or node.kind is not WorkflowNodeKind.WORKFLOW:
                raise ConfigurationError(
                    "workflow checkpoint pause references an invalid child node"
                )
            child_id = str((state.pause or {}).get("child_workflow_id") or "")
            if not child_id:
                raise ConfigurationError("child workflow pause is missing its checkpoint identity")
            paused = await self._recover_child_workflow(state, node, child_id, emit)
            if paused:
                return state
            state.status = WorkflowStatus.RUNNING
            state.pause = None
            return await self._drive(manifest, state, emit, validate_input=False)
        if state.status is WorkflowStatus.AWAITING_APPROVAL:
            if self._approval_is_expired(state) and node is not None:
                await self._expire_approval(state, node, emit)
                return await self._drive(manifest, state, emit, validate_input=False)
            return state
        if state.status in {
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
            WorkflowStatus.AWAITING_INPUT,
        }:
            return state
        unsafe: list[str] = []
        for node_id, execution in state.nodes.items():
            if execution.status is not WorkflowNodeStatus.RUNNING:
                continue
            if nodes[node_id].kind is WorkflowNodeKind.WORKFLOW and execution.child_workflow_ids:
                paused = await self._recover_child_workflow(
                    state,
                    nodes[node_id],
                    execution.child_workflow_ids[-1],
                    emit,
                )
                if paused:
                    return state
                if state.nodes[node_id].status is WorkflowNodeStatus.COMPLETED:
                    continue
                unsafe.append(node_id)
                continue
            if nodes[node_id].recovery_policy == "retry":
                execution.status = WorkflowNodeStatus.PENDING
                execution.error = "interrupted execution scheduled for declared recovery retry"
                execution.started_at = None
                execution.completed_at = None
            else:
                execution.status = WorkflowNodeStatus.FAILED
                execution.error = "interrupted execution is not retry-safe"
                execution.completed_at = utc_now()
                unsafe.append(node_id)
        if unsafe:
            state.status = WorkflowStatus.FAILED
            state.error = f"workflow recovery refused unsafe nodes: {unsafe}"
            for node_id, execution in state.nodes.items():
                if execution.status is not WorkflowNodeStatus.PENDING:
                    continue
                execution.status = WorkflowNodeStatus.SKIPPED
                execution.error = "workflow recovery failed before this node could start"
                execution.completed_at = utc_now()
                await self._emit(emit, "workflow.node.skipped", state, nodes[node_id])
            await self._emit(emit, "workflow.recovery.failed", state, None, {"nodes": unsafe})
            self._checkpoint_locks.pop(state.id, None)
            return state
        state.status = WorkflowStatus.RUNNING
        await self._emit(emit, "workflow.recovered", state, None)
        return await self._drive(manifest, state, emit, validate_input=False)

    async def _recover_child_workflow(
        self,
        state: WorkflowExecutionState,
        node: WorkflowNode,
        child_id: str,
        emit: WorkflowEventSink | None,
    ) -> bool:
        if self._workflow_registry is None:
            raise ConfigurationError("child workflow recovery requires a WorkflowRegistry")
        assert node.workflow_name is not None and node.workflow_version is not None
        child_manifest, child_hooks = self._workflow_registry.resolve(
            node.workflow_name, node.workflow_version
        )

        async def child_event(event_type: str, data: dict[str, Any]) -> None:
            await self._emit(
                emit,
                "workflow.child.event",
                state,
                node,
                {
                    "child_event_type": event_type,
                    "child_workflow_id": child_id,
                    **(
                        {"child_node_id": str(data["node_id"])}
                        if data.get("node_id") is not None
                        else {}
                    ),
                },
            )

        child_executor = MultiAgentWorkflowExecutor(
            self._runtime,
            child_hooks,
            agent_runner=self._agent_runner,
            store=self._store,
            workflow_registry=self._workflow_registry,
            maximum_workflow_depth=self._maximum_workflow_depth,
        )
        recorded_child = await self._store.get(child_id, state.tenant_id)
        if recorded_child is None:
            child_values = state.nodes[node.id].child_workflow_inputs.get(child_id)
            if child_values is None:
                raise ConfigurationError(
                    f"recorded child workflow {child_id!r} has no recoverable input checkpoint"
                )
            child_identity = f"{child_manifest.name}@{child_manifest.version}"
            if child_identity in state.workflow_ancestry:
                raise ConfigurationError(
                    f"workflow dispatch cycle detected through {child_identity!r}"
                )
            if state.workflow_depth + 1 > self._maximum_workflow_depth:
                raise ConfigurationError(
                    f"workflow nesting exceeds maximum depth {self._maximum_workflow_depth}"
                )
            child = await child_executor.run(
                child_manifest,
                child_values,
                tenant_id=state.tenant_id,
                user_id=state.user_id,
                conversation_id=state.conversation_id,
                turn_id=state.turn_id,
                correlation_id=state.correlation_id,
                workflow_id=child_id,
                parent_workflow_id=state.id,
                parent_workflow_node_id=node.id,
                root_workflow_id=state.root_workflow_id or state.id,
                workflow_depth=state.workflow_depth + 1,
                workflow_ancestry=(*state.workflow_ancestry, child_identity),
                emit=child_event,
            )
        else:
            child = await child_executor.recover(
                child_manifest,
                child_id,
                tenant_id=state.tenant_id,
                emit=child_event,
            )
        try:
            paused, result = await self._consume_child_state(
                state, node, child_manifest, child, emit
            )
        except Exception as exc:
            execution = state.nodes[node.id]
            execution.status = WorkflowNodeStatus.FAILED
            execution.error = f"{type(exc).__name__}: {exc}"
            execution.completed_at = utc_now()
            await self._emit(
                emit,
                "workflow.node.failed",
                state,
                node,
                {"error": execution.error},
            )
            return False
        if paused:
            return True
        state.values[node.output_key] = result
        execution = state.nodes[node.id]
        execution.status = WorkflowNodeStatus.COMPLETED
        execution.error = None
        execution.completed_at = utc_now()
        await self._emit(emit, "workflow.node.completed", state, node)
        return False

    async def get(self, workflow_id: str, tenant_id: str) -> WorkflowExecutionState:
        return await self._require_state(workflow_id, tenant_id)

    async def _drive(
        self,
        manifest: WorkflowManifest,
        state: WorkflowExecutionState,
        emit: WorkflowEventSink | None,
        *,
        validate_input: bool,
    ) -> WorkflowExecutionState:
        try:
            remaining = (state.deadline_at - utc_now()).total_seconds()
            if remaining <= 0:
                raise TimeoutError("workflow deadline expired")
            async with asyncio.timeout(remaining):
                if validate_input and manifest.input_schema is not None:
                    if "inputs" not in state.values:
                        raise ConfigurationError(
                            "workflow declares input_schema but state.values['inputs'] is missing"
                        )
                    self._validate_contract(
                        manifest.input_schema,
                        state.values["inputs"],
                        contract="input",
                    )
                await self._schedule(manifest, state, emit)
                if state.status == WorkflowStatus.RUNNING and manifest.output_schema is not None:
                    assert manifest.output_key is not None
                    if manifest.output_key not in state.values:
                        raise ConfigurationError(
                            f"workflow output {manifest.output_key!r} was not produced"
                        )
                    self._validate_contract(
                        manifest.output_schema,
                        state.values[manifest.output_key],
                        contract="output",
                    )
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
            self._checkpoint_locks.pop(state.id, None)
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
        if state.status in {
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        }:
            self._checkpoint_locks.pop(state.id, None)
        return state

    async def _require_state(self, workflow_id: str, tenant_id: str) -> WorkflowExecutionState:
        state = await self._store.get(workflow_id, tenant_id)
        if state is None:
            raise NotFoundError(f"workflow {workflow_id!r} not found")
        return state

    @staticmethod
    def _validate_manifest_identity(
        manifest: WorkflowManifest, state: WorkflowExecutionState
    ) -> None:
        if (state.manifest_name, state.manifest_version) != (manifest.name, manifest.version):
            raise ConflictError(
                "workflow checkpoint manifest identity does not match the supplied manifest"
            )
        if (
            state.manifest_fingerprint is not None
            and state.manifest_fingerprint
            != MultiAgentWorkflowExecutor._manifest_fingerprint(manifest)
        ):
            raise ConflictError("workflow checkpoint manifest fingerprint does not match")

    @staticmethod
    def _manifest_fingerprint(manifest: WorkflowManifest) -> str:
        payload = json.dumps(
            manifest.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _validate_contract(schema: dict[str, Any], value: Any, *, contract: str) -> None:
        errors = sorted(
            Draft202012Validator(schema).iter_errors(value),
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        )
        if not errors:
            return
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path)
        location = f" at {path}" if path else ""
        raise ConfigurationError(
            f"workflow {contract} validation failed{location}: {error.message}"
        )

    async def _schedule(
        self,
        manifest: WorkflowManifest,
        state: WorkflowExecutionState,
        emit: WorkflowEventSink | None,
    ) -> None:
        nodes = {node.id: node for node in manifest.nodes}
        semaphore = asyncio.Semaphore(manifest.maximum_concurrency)
        pending = {
            node_id
            for node_id, execution in state.nodes.items()
            if execution.status in {WorkflowNodeStatus.PENDING, WorkflowNodeStatus.RUNNING}
        }
        while pending:
            if state.status in {
                WorkflowStatus.AWAITING_INPUT,
                WorkflowStatus.AWAITING_APPROVAL,
            }:
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
                                state.nodes[dependency].status == WorkflowNodeStatus.REJECTED
                                and node.kind != WorkflowNodeKind.JOIN
                                and not self._rejected_dependency_continues(nodes[dependency])
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
                        state.nodes[dependency].status == WorkflowNodeStatus.REJECTED
                        and (
                            node.kind == WorkflowNodeKind.JOIN
                            or self._rejected_dependency_continues(nodes[dependency])
                        )
                    )
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
        execution.error = None
        execution.started_at = utc_now()
        await self._emit(emit, "workflow.node.started", state, node)
        try:
            if node.kind == WorkflowNodeKind.APPROVAL:
                assert node.approval is not None
                parameters = self._approval_parameters(node, state)
                if node.approval.parameters_schema is not None and parameters is not None:
                    self._validate_contract(
                        node.approval.parameters_schema,
                        parameters,
                        contract=f"approval parameters for {node.id!r}",
                    )
                requested_at = utc_now()
                execution.status = WorkflowNodeStatus.AWAITING_APPROVAL
                state.status = WorkflowStatus.AWAITING_APPROVAL
                state.pause = {
                    "kind": "approval",
                    "node_id": node.id,
                    "prompt": node.approval.prompt,
                    "review": {
                        key: self._path(state.values, key) for key in node.approval.review_from
                    },
                    "redacted_parameters": parameters,
                    "allow_modification": node.approval.allow_modification,
                    "expires_at": (
                        requested_at + timedelta(seconds=node.approval.expires_seconds)
                    ).isoformat(),
                    "requested_at": requested_at.isoformat(),
                }
                await self._emit(
                    emit,
                    "workflow.approval.required",
                    state,
                    node,
                    {
                        "prompt": node.approval.prompt,
                        "allow_modification": node.approval.allow_modification,
                        "expires_at": state.pause["expires_at"],
                    },
                )
                return
            if node.kind == WorkflowNodeKind.WORKFLOW:
                async with semaphore:
                    paused, result = await self._execute_child_workflow(state, node, emit)
                if paused:
                    return
            elif node.kind == WorkflowNodeKind.HANDLER:
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
                    "requested_at": utc_now().isoformat(),
                }
                state.status = WorkflowStatus.AWAITING_INPUT
                await self._emit(emit, "workflow.input.required", state, node, state.pause)
            await self._emit(emit, "workflow.node.completed", state, node)
        except Exception as exc:
            execution.status = WorkflowNodeStatus.FAILED
            execution.error = f"{type(exc).__name__}: {exc}"
            execution.completed_at = utc_now()
            await self._emit(emit, "workflow.node.failed", state, node, {"error": execution.error})

    async def _execute_child_workflow(
        self,
        state: WorkflowExecutionState,
        node: WorkflowNode,
        emit: WorkflowEventSink | None,
    ) -> tuple[bool, Any]:
        if self._workflow_registry is None:
            raise ConfigurationError("workflow node execution requires a WorkflowRegistry")
        assert node.workflow_name is not None and node.workflow_version is not None
        child_manifest, child_hooks = self._workflow_registry.resolve(
            node.workflow_name, node.workflow_version
        )
        child_identity = f"{child_manifest.name}@{child_manifest.version}"
        if child_identity in state.workflow_ancestry:
            raise ConfigurationError(f"workflow dispatch cycle detected through {child_identity!r}")
        child_depth = state.workflow_depth + 1
        if child_depth > self._maximum_workflow_depth:
            raise ConfigurationError(
                f"workflow nesting exceeds maximum depth {self._maximum_workflow_depth}"
            )
        hook_context = self._context(state, node)
        payload = (
            await self._call(self._hooks.builder(node.input_builder), hook_context)
            if node.input_builder
            else self._render_template(node.input_template or "{{input}}", state.values, None)
        )
        child_values = self._child_values(child_manifest, payload)
        child_id = str(uuid4())
        execution = state.nodes[node.id]
        execution.child_workflow_ids.append(child_id)
        execution.child_workflow_inputs[child_id] = child_values
        execution.attempts += 1
        await self._emit(
            emit,
            "workflow.child.started",
            state,
            node,
            {
                "child_workflow_id": child_id,
                "child_workflow_name": child_manifest.name,
                "child_workflow_version": child_manifest.version,
            },
        )

        async def child_event(event_type: str, data: dict[str, Any]) -> None:
            await self._emit(
                emit,
                "workflow.child.event",
                state,
                node,
                {
                    "child_event_type": event_type,
                    "child_workflow_id": child_id,
                    **(
                        {"child_node_id": str(data["node_id"])}
                        if data.get("node_id") is not None
                        else {}
                    ),
                },
            )

        child_executor = MultiAgentWorkflowExecutor(
            self._runtime,
            child_hooks,
            agent_runner=self._agent_runner,
            store=self._store,
            workflow_registry=self._workflow_registry,
            maximum_workflow_depth=self._maximum_workflow_depth,
        )
        child = await child_executor.run(
            child_manifest,
            child_values,
            tenant_id=state.tenant_id,
            user_id=state.user_id,
            conversation_id=state.conversation_id,
            turn_id=state.turn_id,
            correlation_id=state.correlation_id,
            workflow_id=child_id,
            parent_workflow_id=state.id,
            parent_workflow_node_id=node.id,
            root_workflow_id=state.root_workflow_id or state.id,
            workflow_depth=child_depth,
            workflow_ancestry=(*state.workflow_ancestry, child_identity),
            emit=child_event,
        )
        return await self._consume_child_state(state, node, child_manifest, child, emit)

    async def _consume_child_state(
        self,
        state: WorkflowExecutionState,
        node: WorkflowNode,
        child_manifest: WorkflowManifest,
        child: WorkflowExecutionState,
        emit: WorkflowEventSink | None,
    ) -> tuple[bool, Any]:
        if child.status in {WorkflowStatus.AWAITING_INPUT, WorkflowStatus.AWAITING_APPROVAL}:
            child_pause = child.pause or {}
            state.status = child.status
            state.nodes[node.id].status = (
                WorkflowNodeStatus.AWAITING_APPROVAL
                if child.status is WorkflowStatus.AWAITING_APPROVAL
                else WorkflowNodeStatus.RUNNING
            )
            state.pause = {
                "kind": "child_workflow",
                "node_id": node.id,
                "child_workflow_id": child.id,
                "child_workflow_name": child_manifest.name,
                "child_workflow_version": child_manifest.version,
                "child_status": child.status.value,
                "child_pause": child_pause,
                "requested_at": child_pause.get("requested_at", utc_now().isoformat()),
                **{
                    key: child_pause[key]
                    for key in (
                        "prompt",
                        "review",
                        "redacted_parameters",
                        "allow_modification",
                        "expires_at",
                        "question",
                        "state",
                    )
                    if key in child_pause
                },
            }
            await self._emit(
                emit,
                "workflow.child.paused",
                state,
                node,
                {
                    "child_workflow_id": child.id,
                    "child_status": child.status.value,
                },
            )
            return True, None
        if child.status is not WorkflowStatus.COMPLETED:
            await self._emit(
                emit,
                "workflow.child.failed",
                state,
                node,
                {"child_workflow_id": child.id, "child_status": child.status.value},
            )
            raise RuntimeError(child.error or f"child workflow {child_manifest.name!r} failed")
        result = (
            child.values[child_manifest.output_key]
            if child_manifest.output_key is not None
            else child.values
        )
        await self._emit(
            emit,
            "workflow.child.completed",
            state,
            node,
            {"child_workflow_id": child.id},
        )
        return False, result

    @staticmethod
    def _child_values(manifest: WorkflowManifest, payload: Any) -> dict[str, Any]:
        if manifest.input_schema is not None:
            return {"inputs": payload, "clarifications": []}
        if isinstance(payload, Mapping):
            return {**dict(payload), "clarifications": list(payload.get("clarifications", []))}
        text = str(payload)
        return {"input": text, "question": text, "clarifications": []}

    @classmethod
    def _approval_parameters(
        cls, node: WorkflowNode, state: WorkflowExecutionState
    ) -> dict[str, Any] | None:
        assert node.approval is not None
        if node.approval.parameters_from is None:
            return None
        try:
            parameters = cls._path(state.values, node.approval.parameters_from)
        except (KeyError, AttributeError, IndexError) as exc:
            raise ConfigurationError(
                f"approval parameters path {node.approval.parameters_from!r} was not found"
            ) from exc
        if not isinstance(parameters, Mapping):
            raise ConfigurationError(
                f"approval parameters path {node.approval.parameters_from!r} is not an object"
            )
        return dict(parameters)

    @staticmethod
    def _rejected_dependency_continues(node: WorkflowNode) -> bool:
        return node.approval is not None and node.approval.rejection_policy == "continue"

    @staticmethod
    def _approval_is_expired(state: WorkflowExecutionState) -> bool:
        expires_at = (state.pause or {}).get("expires_at")
        if not isinstance(expires_at, str):
            return False
        try:
            expiry = datetime.fromisoformat(expires_at)
        except ValueError:
            return True
        return expiry.tzinfo is None or expiry <= utc_now()

    @staticmethod
    def _extend_deadline_for_pause(state: WorkflowExecutionState) -> None:
        requested_at = (state.pause or {}).get("requested_at")
        if not isinstance(requested_at, str):
            return
        try:
            paused_at = datetime.fromisoformat(requested_at)
        except ValueError:
            return
        if paused_at.tzinfo is None:
            return
        paused_for = utc_now() - paused_at
        if paused_for.total_seconds() > 0:
            state.deadline_at += paused_for

    async def _expire_approval(
        self,
        state: WorkflowExecutionState,
        node: WorkflowNode,
        emit: WorkflowEventSink | None,
    ) -> None:
        execution = state.nodes[node.id]
        execution.status = WorkflowNodeStatus.FAILED
        execution.error = "workflow approval expired"
        execution.completed_at = utc_now()
        state.pause = None
        state.status = WorkflowStatus.RUNNING
        await self._emit(
            emit,
            "workflow.approval.expired",
            state,
            node,
            {"error": execution.error},
        )

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
                correlation_id=state.correlation_id or state.id,
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

    async def _emit(
        self,
        sink: WorkflowEventSink | None,
        event_type: str,
        state: WorkflowExecutionState,
        node: WorkflowNode | None,
        data: dict[str, Any] | None = None,
    ) -> None:
        await self._checkpoint(state)
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

    async def _checkpoint(self, state: WorkflowExecutionState) -> None:
        lock = self._checkpoint_locks.setdefault(state.id, asyncio.Lock())
        async with lock:
            state.updated_at = utc_now()
            await self._store.save(state, state.version)
