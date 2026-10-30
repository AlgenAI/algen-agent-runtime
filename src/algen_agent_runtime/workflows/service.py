"""The configured workflows as a service: start a run, follow it, answer its approvals and questions.

A deployed project's other systems (a mail worker, a scheduler, an HR system's approvals inbox)
need to start its workflows and decide their approval steps without a person in Studio. The
service runs ``settings.workflows`` with the Runtime's own executor and checkpoint store, so a
run survives a restart when the store is PostgreSQL, and every call is scoped to the caller's
tenant. The service owns the runs it starts (``owner="workflow_service"``): on start the container
recovers the ones a stop interrupted (``recover``), the maintenance sweep expires their approval
and input waits (``expire_paused``), and a drain leaves unfinished runs recoverable rather than
cancelled (``drain``). Hook code (custom steps) is imported only from modules under
``security.trusted_plugin_prefixes``, the same trust that plugins need; a workflow whose hooks
are not trusted cannot be started.

When a run waits for an approval or input, or ends, an optional webhook
(``workflow_notifications``) is sent a small JSON event, signed with HMAC-SHA256 under an
``env://`` secret, so the other system can show the approval and answer it through the API.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.exceptions.errors import (
    ConfigurationError,
    ConflictError,
    NotFoundError,
)
from algen_agent_runtime.plugins.signing import verify_module
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.security.network import create_safe_http_client
from algen_agent_runtime.workflows.contracts import (
    WorkflowExecutionState,
    WorkflowManifest,
    WorkflowStatus,
)
from algen_agent_runtime.workflows.engine import (
    MultiAgentWorkflowExecutor,
    WorkflowHookRegistry,
    WorkflowRegistry,
)
from algen_agent_runtime.workflows.loader import LoadedHookProvider, WorkflowHookLoader

LOGGER = logging.getLogger(__name__)
# Events a notification webhook receives.
NOTIFIED = {
    WorkflowStatus.AWAITING_APPROVAL: "workflow.awaiting_approval",
    WorkflowStatus.AWAITING_INPUT: "workflow.awaiting_input",
    WorkflowStatus.COMPLETED: "workflow.completed",
    WorkflowStatus.FAILED: "workflow.failed",
}
SIGNATURE_HEADER = "X-Algen-Signature"
# The owner recorded on runs this service starts; only these are recovered and expired by it.
OWNER = "workflow_service"
PAUSED = {WorkflowStatus.AWAITING_APPROVAL, WorkflowStatus.AWAITING_INPUT}
INTERRUPTED = {WorkflowStatus.PENDING, WorkflowStatus.RUNNING}


class WorkflowService:
    """Starts and continues the configured workflows for API callers."""

    def __init__(self, settings: AppSettings, container: Any) -> None:
        self._settings, self._container = settings, container
        self._loaded: list[LoadedHookProvider] = []
        self._hooks: dict[str, WorkflowHookRegistry] = {}
        self._registry: WorkflowRegistry | None = None
        self._untrusted: dict[str, str] = {}
        # Hook module -> why its signature was refused, or None once it verified.
        self._signatures: dict[str, str | None] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        # (tenant_id, workflow_id) of runs a task in this process is driving.
        self._active: set[tuple[str, str]] = set()
        self._ready = asyncio.Lock()
        self._draining = False

    def catalog(self) -> list[dict[str, Any]]:
        """Each configured workflow: its name, version, input schema and whether it can run."""
        return [
            {
                "name": name,
                "version": manifest.version,
                "description": manifest.description,
                "input_schema": manifest.input_schema,
                "available": self._trusted(manifest) is None,
            }
            for name, manifest in sorted(self._settings.workflows.items())
        ]

    async def start(
        self,
        name: str,
        *,
        tenant_id: str,
        user_id: str,
        input_text: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        workflow_id: str | None = None,
    ) -> WorkflowExecutionState:
        """Start a run of the workflow; it carries on in the background."""
        manifest = self._manifest(name)
        if problem := self._trusted(manifest):
            raise ConfigurationError(problem)
        self._require_open()
        executor = await self._executor(name)
        run_id = workflow_id or str(uuid4())
        values: dict[str, Any] = {"input": input_text or json.dumps(dict(inputs or {}))}
        if inputs is not None:
            values["inputs"] = dict(inputs)
        self._spawn(
            executor.run(
                manifest,
                values,
                tenant_id=tenant_id,
                user_id=user_id,
                workflow_id=run_id,
                owner=OWNER,
            ),
            name,
            (tenant_id, run_id),
        )
        return await self._wait_for_state(run_id, tenant_id)

    def manifest_or_none(self, name: str) -> WorkflowManifest | None:
        return self._settings.workflows.get(name)

    async def find(self, workflow_id: str, tenant_id: str) -> WorkflowExecutionState | None:
        """The run with this id in the tenant, if it exists."""
        state = await self._container.workflow_checkpoints.get(workflow_id, tenant_id)
        return state  # type: ignore[no-any-return]

    async def status(self, workflow_id: str, tenant_id: str) -> WorkflowExecutionState:
        state = await self._container.workflow_checkpoints.get(workflow_id, tenant_id)
        if state is None:
            raise NotFoundError(f"workflow run {workflow_id!r} not found")
        return state  # type: ignore[no-any-return]

    async def decide(
        self,
        workflow_id: str,
        *,
        tenant_id: str,
        user_id: str,
        decision: str,
        comment: str | None = None,
        modified_parameters: Mapping[str, Any] | None = None,
    ) -> WorkflowExecutionState:
        """Answer the approval step a run waits on; the run carries on in the background."""
        state = await self.status(workflow_id, tenant_id)
        manifest = self._manifest(state.manifest_name)
        executor = await self._executor(state.manifest_name)
        if state.status is not WorkflowStatus.AWAITING_APPROVAL:
            raise ConfigurationError(f"workflow run {workflow_id!r} is not awaiting approval")
        self._require_open()
        self._spawn(
            executor.decide_approval(
                manifest,
                workflow_id,
                tenant_id=tenant_id,
                user_id=user_id,
                decision=decision,
                comment=comment,
                modified_parameters=modified_parameters,
            ),
            state.manifest_name,
            (tenant_id, workflow_id),
        )
        return await self._wait_for_change(state)

    async def provide_input(
        self, workflow_id: str, *, tenant_id: str, values: Mapping[str, Any]
    ) -> WorkflowExecutionState:
        """Answer the question a paused step asked; the run carries on in the background."""
        state = await self.status(workflow_id, tenant_id)
        if state.status is not WorkflowStatus.AWAITING_INPUT:
            raise ConfigurationError(f"workflow run {workflow_id!r} is not awaiting input")
        self._require_open()
        executor = await self._executor(state.manifest_name)
        self._spawn(
            executor.resume(
                self._manifest(state.manifest_name),
                workflow_id,
                tenant_id=tenant_id,
                values=dict(values),
            ),
            state.manifest_name,
            (tenant_id, workflow_id),
        )
        return await self._wait_for_change(state)

    async def recover(self, limit: int = 1000) -> int:
        """Carry on the runs this service started that a stop interrupted; returns how many.

        Only top-level runs with ``owner="workflow_service"`` that were pending or running are
        taken up (their child workflows are recovered through them). Each is driven with the
        exact configured manifest, name and version, whose hooks come only from trusted modules;
        a run whose manifest is no longer configured, changed version or is untrusted is left
        alone and logged. Recovery uses the engine's ``recover``, so an interrupted node is
        retried only when its ``recovery_policy`` is ``retry``.
        """
        started = 0
        for state in await self._container.workflow_checkpoints.list_recoverable(limit):
            if state.status not in INTERRUPTED or not self._takes(state):
                continue
            manifest = self._runnable_manifest(state)
            if manifest is None:
                continue
            executor = await self._executor(state.manifest_name)
            self._spawn(
                executor.recover(manifest, state.id, tenant_id=state.tenant_id),
                state.manifest_name,
                (state.tenant_id, state.id),
            )
            started += 1
        if started:
            LOGGER.info("recovering %d interrupted workflow run(s)", started)
        return started

    async def expire_paused(
        self, *, limit: int = 1000, input_timeout_seconds: float | None = None
    ) -> int:
        """Expire this service's approval and input waits that are past expiry; returns how many.

        Approval waits expire at their ``expires_at``; input waits only with
        ``input_timeout_seconds``. The engine's ``expire_pause`` does the work, so the expired step
        fails exactly as a late answer would find it, the run continues under its failure and
        rejection policies, and the result is checkpointed (and notified) as usual. Runs already
        being driven here are skipped, and saves are version-checked, so repeating a sweep, or
        sweeping from several replicas, expires each wait once. Every run is handled in its own
        tenant. At most ``limit`` unfinished runs, oldest first, are looked at per call.
        """
        expired = 0
        for state in await self._container.workflow_checkpoints.list_recoverable(limit):
            if state.status not in PAUSED or not self._takes(state):
                continue
            if not MultiAgentWorkflowExecutor.pause_expired(state, input_timeout_seconds):
                continue
            manifest = self._runnable_manifest(state)
            if manifest is None:
                continue
            executor = await self._executor(state.manifest_name)
            self._spawn(
                executor.expire_pause(
                    manifest,
                    state.id,
                    tenant_id=state.tenant_id,
                    input_timeout_seconds=input_timeout_seconds,
                ),
                state.manifest_name,
                (state.tenant_id, state.id),
            )
            expired += 1
        return expired

    async def idle(self) -> None:
        """Wait until every run this process is driving has finished or paused."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    @property
    def draining(self) -> bool:
        return self._draining

    async def drain(self, grace_seconds: float = 0) -> None:
        """Stop taking work, let running runs carry on for the grace period, then interrupt them.

        An interrupted run keeps its last checkpoint (it is not marked cancelled), so the next
        start recovers it. Starting or answering a run while draining raises ``ConflictError``.
        """
        self._draining = True
        pending = set(self._tasks)
        if pending and grace_seconds > 0:
            _, pending = await asyncio.wait(pending, timeout=grace_seconds)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def aclose(self, grace_seconds: float = 0) -> None:
        """Drain, then close the loaded hook providers; safe to call more than once."""
        await self.drain(grace_seconds)
        loaded, self._loaded = self._loaded, []
        for provider in reversed(loaded):
            await provider.aclose()

    def _require_open(self) -> None:
        if self._draining:
            raise ConflictError("the workflow service is draining and is not taking new work")

    def _takes(self, state: WorkflowExecutionState) -> bool:
        """Whether this service drives the run: one it started, top-level, not running here."""
        return (
            not self._draining
            and state.owner == OWNER
            and state.parent_workflow_id is None
            and (state.tenant_id, state.id) not in self._active
        )

    def _runnable_manifest(self, state: WorkflowExecutionState) -> WorkflowManifest | None:
        """The exact configured manifest of the run, when it may be driven here."""
        manifest = self._settings.workflows.get(state.manifest_name)
        if manifest is None or not MultiAgentWorkflowExecutor.manifest_matches(manifest, state):
            LOGGER.warning(
                "workflow run %s: %s@%s is not configured here (or its manifest changed); "
                "left for an operator",
                state.id,
                state.manifest_name,
                state.manifest_version,
            )
            return None
        if self._trusted(manifest) is not None:
            LOGGER.warning(
                "workflow run %s: its hooks are not under security.trusted_plugin_prefixes; "
                "left for an operator",
                state.id,
            )
            return None
        return manifest

    def _manifest(self, name: str) -> WorkflowManifest:
        try:
            return self._settings.workflows[name]
        except KeyError as error:
            raise NotFoundError(f"workflow {name!r} is not configured") from error

    def _trusted(self, manifest: WorkflowManifest) -> str | None:
        """Why the workflow's hook code may not be loaded, or None."""
        if manifest.hook_provider is None:
            return None
        module = manifest.hook_provider.split(":", 1)[0]
        loader = WorkflowHookLoader(allowed_modules=self._settings.security.trusted_plugin_prefixes)
        if not loader.is_module_allowed(module):
            return (
                f"workflow {manifest.name!r} runs code from {module!r}, which is not under "
                "security.trusted_plugin_prefixes"
            )
        signing = self._settings.plugins
        if not signing.enabled:
            return None
        if module not in self._signatures:
            # Verified before the hook module is imported, like a plugin.
            try:
                verify_module(module, signing)
                self._signatures[module] = None
            except ConfigurationError as error:
                self._signatures[module] = str(error)
        return self._signatures[module]

    async def _executor(self, name: str) -> MultiAgentWorkflowExecutor:
        async with self._ready:
            if self._registry is None:
                await self._load_hooks()
        assert self._registry is not None
        return MultiAgentWorkflowExecutor(
            AlgenAgentRuntimeClient(self._container.runtime),
            self._hooks.get(name, WorkflowHookRegistry()),
            store=self._container.workflow_checkpoints,
            workflow_registry=self._registry,
            interrupted=lambda: self._draining,
        )

    async def _load_hooks(self) -> None:
        """Every trusted workflow's hooks, once, so child workflows resolve too."""
        registry = WorkflowRegistry()
        loader = WorkflowHookLoader(allowed_modules=self._settings.security.trusted_plugin_prefixes)
        for name, manifest in self._settings.workflows.items():
            hooks = WorkflowHookRegistry()
            if manifest.hook_provider is not None and self._trusted(manifest) is None:
                provider = await loader.aload(
                    manifest.hook_provider,
                    container=self._container,
                    manifest=manifest,
                    environment=dict(os.environ),
                )
                self._loaded.append(provider)
                hooks = provider.hooks
            registry.register(manifest, hooks)
            self._hooks[name] = hooks
        self._registry = registry

    def _spawn(self, work: Any, name: str, key: tuple[str, str]) -> None:
        async def drive() -> None:
            try:
                state = await work
            except Exception:
                LOGGER.exception("workflow %s stopped with an error", name)
                self._active.discard(key)
                return
            self._active.discard(key)
            await self._notify(state)

        self._active.add(key)
        task = asyncio.create_task(drive())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _: self._active.discard(key))

    async def _wait_for_state(self, workflow_id: str, tenant_id: str) -> WorkflowExecutionState:
        for _ in range(200):
            state = await self._container.workflow_checkpoints.get(workflow_id, tenant_id)
            if state is not None:
                return state  # type: ignore[no-any-return]
            await asyncio.sleep(0.01)
        raise ConfigurationError(f"workflow run {workflow_id!r} did not start")

    async def _wait_for_change(self, before: WorkflowExecutionState) -> WorkflowExecutionState:
        """The run once the decision was taken up (its checkpoint moved on)."""
        for _ in range(200):
            state = await self.status(before.id, before.tenant_id)
            if state.version != before.version:
                return state
            await asyncio.sleep(0.01)
        return before

    async def _notify(self, state: WorkflowExecutionState) -> None:
        """Tell the configured webhook that a run waits or ended; never fails the run."""
        settings = self._settings.workflow_notifications
        event = NOTIFIED.get(state.status)
        if settings is None or event is None or event not in settings.events:
            return
        body = json.dumps(
            {
                "event": event,
                "workflow_id": state.id,
                "workflow": state.manifest_name,
                "tenant_id": state.tenant_id,
                "pause": public_pause(state.pause),
                "error": state.error if state.status is WorkflowStatus.FAILED else None,
            },
            sort_keys=True,
        ).encode()
        headers = {"Content-Type": "application/json"}
        if settings.signing_secret:
            secret = os.environ.get(settings.signing_secret.removeprefix("env://"), "")
            if not secret:
                LOGGER.warning("workflow %s: %s is not set; not notified", state.id, event)
                return
            digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            headers[SIGNATURE_HEADER] = f"sha256={digest}"
        security = self._settings.security
        try:
            async with create_safe_http_client(
                allowed_hosts=tuple(security.allowed_http_hosts),
                allow_private_networks=security.allow_private_networks,
                timeout=10.0,
                outbound=self._container.outbound,
            ) as client:
                response = await client.post(settings.webhook_url, content=body, headers=headers)
                response.raise_for_status()
        except Exception:
            LOGGER.warning("workflow %s: the %s notification was not delivered", state.id, event)


def public_pause(pause: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What a waiting step asks, without the run's values."""
    if not pause:
        return None
    keep = (
        "kind",
        "node_id",
        "prompt",
        "question",
        "review",
        "redacted_parameters",
        "allow_modification",
        "requested_at",
        "expires_at",
    )
    return {key: pause[key] for key in keep if key in pause}
