from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from fastapi.responses import FileResponse

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from examples.case_study_teaching_assistant.app import CONFIG_PATH, UI_PATH
from examples.case_study_teaching_assistant.application import (
    AcademicIntegrityPolicy,
    EducationalEquityPolicy,
    TeachingAssistantConversationHandler,
    register_teaching_tools,
)


def build_dashboard_app() -> object:
    extra_config = os.getenv("TEACHING_ASSISTANT_CONFIG")
    files = (CONFIG_PATH, Path(extra_config)) if extra_config else (CONFIG_PATH,)
    settings = load_settings(files)
    container = build_container(settings)
    register_teaching_tools(container.tools)
    integrity = AcademicIntegrityPolicy()
    equity = EducationalEquityPolicy()
    container.runtime.policies.register(integrity)
    container.runtime.policies.register(equity)
    run = container.observability.govern(
        AlgenAgentRuntimeClient(container.runtime).run,
        agent_id="virtual-teaching-assistant",
        agent_name="virtual-teaching-assistant",
    )
    container.conversations.handlers.register(
        TeachingAssistantConversationHandler(
            run,
            academic_integrity=integrity,
            educational_equity=equity,
        )
    )
    app = create_app(settings, container)

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(UI_PATH)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(UI_PATH, media_type="text/html")

    return app


app = build_dashboard_app()


def main() -> None:
    settings = load_settings((CONFIG_PATH,))
    uvicorn.run(
        "examples.case_study_teaching_assistant.dashboard:app",
        host=settings.api.host,
        port=int(os.getenv("PORT", str(settings.api.port))),
    )


if __name__ == "__main__":
    main()
