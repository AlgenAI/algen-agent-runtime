"""The configured workflows as a service: start a run, follow it, answer its approvals and questions.

A deployed project's other systems (a mail worker, a scheduler, an HR system's approvals inbox)
need to start its workflows and decide their approval steps without a person in Studio. The
service runs ``settings.workflows`` with the Runtime's own executor and checkpoint store, so a
run survives a restart when the store is PostgreSQL, and every call is scoped to the caller's
tenant. Hook code (custom steps) is imported only from modules under
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
from algen_agent_runtime.exceptions.errors import ConfigurationError, NotFoundError
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


class WorkflowService:
    """Starts and continues the configured workflows for API callers."""

    def __init__(self, settings: AppSettings, container: Any) -> None:
        self._settings, self._container = settings, container
        self._loaded: list[LoadedHookProvider] = []
        self._hooks: dict[str, WorkflowHookRegistry] = {}
        self._registry: WorkflowRegistry | None = None
        self._untrusted: dict[str, str] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._ready = asyncio.Lock()

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
        executor = await self._executor(name)
        run_id = workflow_id or str(uuid4())
        values: dict[str, Any] = {"input": input_text or json.dumps(dict(inputs or {}))}
        if inputs is not None:
            values["inputs"] = dict(inputs)
        self._spawn(
            executor.run(
                manifest, values, tenant_id=tenant_id, user_id=user_id, workflow_id=run_id
            ),
            name,
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
        )
        return await self._wait_for_change(state)

    async def provide_input(
        self, workflow_id: str, *, tenant_id: str, values: Mapping[str, Any]
    ) -> WorkflowExecutionState:
        """Answer the question a paused step asked; the run carries on in the background."""
        state = await self.status(workflow_id, tenant_id)
        if state.status is not WorkflowStatus.AWAITING_INPUT:
            raise ConfigurationError(f"workflow run {workflow_id!r} is not awaiting input")
        executor = await self._executor(state.manifest_name)
        self._spawn(
            executor.resume(
                self._manifest(state.manifest_name),
                workflow_id,
                tenant_id=tenant_id,
                values=dict(values),
            ),
            state.manifest_name,
        )
        return await self._wait_for_change(state)

    async def aclose(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for provider in reversed(self._loaded):
            await provider.aclose()

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
        if loader.is_module_allowed(module):
            return None
        return (
            f"workflow {manifest.name!r} runs code from {module!r}, which is not under "
            "security.trusted_plugin_prefixes"
        )

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

    def _spawn(self, work: Any, name: str) -> None:
        async def drive() -> None:
            try:
                state = await work
            except Exception:
                LOGGER.exception("workflow %s stopped with an error", name)
                return
            await self._notify(state)

        task = asyncio.create_task(drive())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
