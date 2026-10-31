"""HTTP routes for the configured workflows (``workflows/service.py``).

``GET /v1/workflows`` lists them; ``POST /v1/workflows/{name}/runs`` starts one;
``GET /v1/workflows/runs/{workflow_id}`` reads a run; ``.../approval`` answers the approval step
it waits on and ``.../input`` the question a paused step asked. Starting and answering need the
``runs:write`` scope and reading ``runs:read``, as for agent runs; every call sees only its
caller's tenant.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, status
from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.api.dependencies import Principal, require_scope
from algen_agent_runtime.types.contracts import RunAttachment
from algen_agent_runtime.workflows.contracts import WorkflowExecutionState, WorkflowStatus
from algen_agent_runtime.workflows.service import WorkflowService, public_pause


class StartWorkflowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: str | None = Field(default=None, max_length=100_000)
    inputs: dict[str, Any] | None = None
    # A caller-chosen id makes a retried start find the run it already started.
    workflow_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{8,128}$")
    # Uploaded files (POST /v1/artifacts), for the nodes that set pass_attachments.
    attachments: tuple[RunAttachment, ...] = Field(default=(), max_length=32)


class WorkflowApprovalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approved", "rejected", "modified"]
    comment: str | None = Field(default=None, max_length=2_000)
    modified_parameters: dict[str, Any] | None = None


class WorkflowInputBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, Any]


def run_view(state: WorkflowExecutionState, output_key: str | None) -> dict[str, Any]:
    """A run as the API shows it: where it stands, what it waits for, and its result."""
    done = state.status is WorkflowStatus.COMPLETED
    return {
        "workflow_id": state.id,
        "workflow": state.manifest_name,
        "version": state.manifest_version,
        "status": state.status.value,
        "pause": public_pause(state.pause),
        "error": state.error,
        "output": state.values.get(output_key) if done and output_key else None,
        "steps": {node_id: node.status.value for node_id, node in state.nodes.items()},
        "created_at": state.created_at.isoformat(),
        "updated_at": state.updated_at.isoformat(),
    }


def add_workflow_routes(app: FastAPI, service: WorkflowService, identity: Any) -> None:
    def output_key(name: str) -> str | None:
        manifest = service.manifest_or_none(name)
        return manifest.output_key if manifest else None

    @app.get("/v1/workflows")
    async def list_workflows(caller: Principal = identity) -> dict[str, Any]:
        require_scope(caller, "runs:read")
        return {"workflows": service.catalog()}

    @app.post("/v1/workflows/{name}/runs", status_code=status.HTTP_202_ACCEPTED)
    async def start_workflow(
        name: str, body: StartWorkflowBody, caller: Principal = identity
    ) -> dict[str, Any]:
        require_scope(caller, "runs:write")
        if body.workflow_id is not None:
            found = await service.find(body.workflow_id, caller.tenant_id)
            if found is not None:
                return run_view(found, output_key(found.manifest_name))
        state = await service.start(
            name,
            tenant_id=caller.tenant_id,
            user_id=caller.user_id,
            input_text=body.input,
            inputs=body.inputs,
            workflow_id=body.workflow_id,
            attachments=body.attachments,
        )
        return run_view(state, output_key(name))

    @app.get("/v1/workflows/runs/{workflow_id}")
    async def read_workflow_run(workflow_id: str, caller: Principal = identity) -> dict[str, Any]:
        require_scope(caller, "runs:read")
        state = await service.status(workflow_id, caller.tenant_id)
        return run_view(state, output_key(state.manifest_name))

    @app.post("/v1/workflows/runs/{workflow_id}/approval", status_code=202)
    async def decide_workflow_approval(
        workflow_id: str, body: WorkflowApprovalBody, caller: Principal = identity
    ) -> dict[str, Any]:
        require_scope(caller, "runs:write")
        state = await service.decide(
            workflow_id,
            tenant_id=caller.tenant_id,
            user_id=caller.user_id,
            decision=body.decision,
            comment=body.comment,
            modified_parameters=body.modified_parameters,
        )
        return run_view(state, output_key(state.manifest_name))

    @app.post("/v1/workflows/runs/{workflow_id}/input", status_code=202)
    async def answer_workflow_question(
        workflow_id: str, body: WorkflowInputBody, caller: Principal = identity
    ) -> dict[str, Any]:
        require_scope(caller, "runs:write")
        state = await service.provide_input(
            workflow_id, tenant_id=caller.tenant_id, values=body.values
        )
        return run_view(state, output_key(state.manifest_name))
