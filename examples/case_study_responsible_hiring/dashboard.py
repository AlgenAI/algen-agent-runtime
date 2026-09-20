from __future__ import annotations

import hashlib
import os
from typing import Any

import uvicorn
from fastapi import Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.api.dependencies import Principal, principal_dependency, require_scope
from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.persistence.postgres import PostgresDatabase
from algen_agent_runtime.retrieval.contracts import SourceDocument
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.security.documents import DocumentDisposition, extractor_for, redact_pii
from examples.case_study_responsible_hiring.app import (
    CONFIG_PATH,
    MIGRATION_PATH,
    TEMPLATE_CORPUS_PATH,
    UI_PATH,
)
from examples.case_study_responsible_hiring.application.contracts import HiringApplication
from examples.case_study_responsible_hiring.application.document_pipeline import (
    candidate_identity_entities,
)
from examples.case_study_responsible_hiring.application.repositories import PostgresHiringRepository
from examples.case_study_responsible_hiring.application.workflow import (
    HiringConversationHandler,
    HiringWorkflow,
)


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern=r"^(advance|decline|review)$")
    rationale: str = Field(min_length=3, max_length=2_000)


def create_hiring_app() -> Any:
    settings = load_settings((CONFIG_PATH,))
    container = build_container(settings)
    database = next(
        (resource for resource in container.resources if isinstance(resource, PostgresDatabase)),
        None,
    )
    if database is None:
        raise RuntimeError("the hiring example requires PostgreSQL storage")
    repository = PostgresHiringRepository(database, MIGRATION_PATH)
    workflow = HiringWorkflow(AlgenAgentRuntimeClient(container.runtime), repository)
    container.conversations.handlers.register(HiringConversationHandler(workflow, repository))
    app = create_app(settings, container)
    authenticate = principal_dependency(settings.api)
    identity_dependency = Depends(authenticate)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(UI_PATH)

    @app.post("/v1/hiring/applications", status_code=201)
    async def create_application(
        candidate_name: str = Form(..., min_length=1, max_length=200),
        job_title: str = Form(..., min_length=1, max_length=200),
        job_description: str = Form(..., min_length=20, max_length=50_000),
        resume_text: str | None = Form(default=None, max_length=250_000),
        resume_file: UploadFile | None = File(default=None),  # noqa: B008
        identity: Principal = identity_dependency,
    ) -> dict[str, Any]:
        require_scope(identity, "conversations:write")
        if resume_file is not None:
            data = await resume_file.read(settings.security.max_request_bytes + 1)
            filename = resume_file.filename or "resume.pdf"
        elif resume_text and resume_text.strip():
            data = resume_text.encode()
            filename = "resume.txt"
        else:
            raise HTTPException(status_code=400, detail="resume text or a resume file is required")
        try:
            extracted = extractor_for(filename).extract(data, filename=filename)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        sanitized = redact_pii(
            extracted.sanitized_text,
            entities=candidate_identity_entities(candidate_name, extracted.sanitized_text),
        )
        application = HiringApplication(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            candidate_name=candidate_name.strip(),
            job_title=job_title.strip(),
            job_description=job_description.strip(),
            sanitized_resume=sanitized,
            document_sha256=hashlib.sha256(data).hexdigest(),
            document_findings=extracted.findings,
            quarantined=extracted.disposition == DocumentDisposition.QUARANTINE,
        )
        await repository.save_application(application)
        await container.retrievers.get("job_requirements").ingest(
            (
                SourceDocument(
                    id=f"job:{application.id}",
                    text=application.job_description,
                    source="hiring-job-description",
                    title=application.job_title,
                    metadata={"tenant_id": identity.tenant_id, "job_id": application.id},
                ),
            )
        )
        await container.retrievers.get("resume_templates").ingest(
            (
                SourceDocument(
                    id="template:generic-demo",
                    text=TEMPLATE_CORPUS_PATH.read_text(encoding="utf-8"),
                    source="bundled-synthetic-template-corpus",
                    title="Synthetic generic resume language",
                    metadata={"tenant_id": identity.tenant_id, "corpus": "demo"},
                ),
            )
        )
        return {
            "id": application.id,
            "quarantined": application.quarantined,
            "findings": [item.model_dump(mode="json") for item in application.document_findings],
            "document_sha256": application.document_sha256,
        }

    @app.post("/v1/hiring/applications/{application_id}/evaluate", status_code=202)
    async def evaluate_application(
        application_id: str, identity: Principal = identity_dependency
    ) -> dict[str, Any]:
        require_scope(identity, "conversations:write")
        application = await repository.application(application_id, identity.tenant_id)
        if application.user_id != identity.user_id:
            raise HTTPException(status_code=404, detail="application not found")
        conversation = await container.conversations.create(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            agent="hiring-orchestrator",
            handler="hiring",
            title=f"{application.job_title} candidate review",
            metadata={"application_id": application.id},
        )
        user_message, assistant_message = await container.conversations.submit(
            conversation.id,
            identity.tenant_id,
            identity.user_id,
            "Evaluate this application against the approved job description.",
            {"application_id": application.id},
        )
        return {
            "conversation_id": conversation.id,
            "message_id": user_message.id,
            "assistant_message_id": assistant_message.id,
        }

    @app.get("/v1/hiring/assessments/{assessment_id}")
    async def get_assessment(
        assessment_id: str, identity: Principal = identity_dependency
    ) -> dict[str, Any]:
        require_scope(identity, "conversations:read")
        assessment = await repository.assessment(assessment_id, identity.tenant_id)
        return assessment.model_dump(mode="json")

    @app.post("/v1/hiring/assessments/{assessment_id}/decision", status_code=201)
    async def record_decision(
        assessment_id: str,
        body: DecisionBody,
        identity: Principal = identity_dependency,
    ) -> dict[str, str]:
        require_scope(identity, "conversations:write")
        await repository.decide(
            assessment_id, identity.tenant_id, identity.user_id, body.decision, body.rationale
        )
        return {"status": "recorded", "decision": body.decision}

    return app


app = create_hiring_app()


def main() -> None:
    uvicorn.run(
        "examples.hiring_agent.dashboard:app",
        host="0.0.0.0",
        port=int(os.getenv("HIRING_AGENT_PORT", "8092")),
        reload=False,
    )


if __name__ == "__main__":
    main()
