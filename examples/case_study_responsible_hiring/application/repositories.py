from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Protocol

from algen_agent_runtime.exceptions.errors import NotFoundError
from examples.case_study_responsible_hiring.application.contracts import (
    HiringApplication,
    HiringAssessment,
)


class HiringRepository(Protocol):
    async def initialize(self) -> None: ...
    async def save_application(self, application: HiringApplication) -> None: ...
    async def application(self, application_id: str, tenant_id: str) -> HiringApplication: ...
    async def save_assessment(self, assessment: HiringAssessment) -> None: ...
    async def assessment(self, assessment_id: str, tenant_id: str) -> HiringAssessment: ...
    async def decide(
        self, assessment_id: str, tenant_id: str, user_id: str, decision: str, rationale: str
    ) -> None: ...


class InMemoryHiringRepository:
    def __init__(self) -> None:
        self.applications: dict[str, HiringApplication] = {}
        self.assessments: dict[str, HiringAssessment] = {}
        self.decisions: list[dict[str, str]] = []
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        return None

    async def save_application(self, application: HiringApplication) -> None:
        async with self._lock:
            self.applications[application.id] = application

    async def application(self, application_id: str, tenant_id: str) -> HiringApplication:
        item = self.applications.get(application_id)
        if item is None or item.tenant_id != tenant_id:
            raise NotFoundError(f"application {application_id!r} not found")
        return item

    async def save_assessment(self, assessment: HiringAssessment) -> None:
        async with self._lock:
            self.assessments[assessment.id] = assessment

    async def assessment(self, assessment_id: str, tenant_id: str) -> HiringAssessment:
        item = self.assessments.get(assessment_id)
        if item is None or item.tenant_id != tenant_id:
            raise NotFoundError(f"assessment {assessment_id!r} not found")
        return item

    async def decide(
        self, assessment_id: str, tenant_id: str, user_id: str, decision: str, rationale: str
    ) -> None:
        await self.assessment(assessment_id, tenant_id)
        self.decisions.append(
            {
                "assessment_id": assessment_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "decision": decision,
                "rationale": rationale,
            }
        )


class PostgresHiringRepository:
    def __init__(self, database: Any, migration: Path) -> None:
        self._database = database
        self._migration = migration
        self._ready = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if not self._ready:
                await self._database.execute(self._migration.read_text(encoding="utf-8"))
                self._ready = True

    async def save_application(self, application: HiringApplication) -> None:
        await self.initialize()
        await self._database.execute(
            "INSERT INTO hiring_applications (id,tenant_id,user_id,application,created_at) VALUES ($1,$2,$3,$4::jsonb,$5)",
            application.id,
            application.tenant_id,
            application.user_id,
            application.model_dump_json(),
            application.created_at,
        )

    async def application(self, application_id: str, tenant_id: str) -> HiringApplication:
        await self.initialize()
        row = await self._database.fetchrow(
            "SELECT application FROM hiring_applications WHERE id=$1 AND tenant_id=$2",
            application_id,
            tenant_id,
        )
        if not row:
            raise NotFoundError(f"application {application_id!r} not found")
        return HiringApplication.model_validate_json(row["application"])

    async def save_assessment(self, assessment: HiringAssessment) -> None:
        await self.initialize()
        await self._database.execute(
            "INSERT INTO hiring_assessments (id,application_id,tenant_id,assessment,created_at) VALUES ($1,$2,$3,$4::jsonb,$5)",
            assessment.id,
            assessment.application_id,
            assessment.tenant_id,
            assessment.model_dump_json(),
            assessment.created_at,
        )

    async def assessment(self, assessment_id: str, tenant_id: str) -> HiringAssessment:
        await self.initialize()
        row = await self._database.fetchrow(
            "SELECT assessment FROM hiring_assessments WHERE id=$1 AND tenant_id=$2",
            assessment_id,
            tenant_id,
        )
        if not row:
            raise NotFoundError(f"assessment {assessment_id!r} not found")
        return HiringAssessment.model_validate_json(row["assessment"])

    async def decide(
        self, assessment_id: str, tenant_id: str, user_id: str, decision: str, rationale: str
    ) -> None:
        await self.assessment(assessment_id, tenant_id)
        await self._database.execute(
            "INSERT INTO hiring_decisions (assessment_id,tenant_id,user_id,decision,rationale) VALUES ($1,$2,$3,$4,$5)",
            assessment_id,
            tenant_id,
            user_id,
            decision,
            rationale,
        )
