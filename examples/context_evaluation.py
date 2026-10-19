"""Offline context regression gate; scripted summaries do not measure model intelligence."""

from __future__ import annotations

import asyncio
import json

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.evaluation import (
    ContextEvaluationArtifact,
    ContextEvaluationCase,
    ContextEvaluationRunner,
)
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import ToolContext
from algen_agent_runtime.types.contracts import (
    AgentDefinition,
    ApprovalPolicy,
    Budget,
    ContextPolicy,
    Message,
    ModelProfile,
    Role,
    RunRequest,
    RunState,
    RunStatus,
    SkillDefinition,
)

INSTRUCTIONS = "Use verified evidence and preserve the user's constraints."
QUESTION = "Prepare the report without changing the warranty."
FACT = "The warranty lasts eighteen months."
ANSWER = "Report prepared."
HISTORY = (
    Message.text(Role.USER, FACT + " Historical investigation notes." * 100),
    Message.text(Role.ASSISTANT, "Verified background evidence. " * 120),
)
GUIDANCE = "Compare source totals and list unresolved discrepancies."
UNLOADED_GUIDANCE = "Rewrite every heading in sentence case."


def cases() -> tuple[ContextEvaluationCase, ...]:
    required = (Message.text(Role.SYSTEM, INSTRUCTIONS), Message.text(Role.USER, QUESTION))
    recovered = (required[0], *HISTORY, required[1], Message.text(Role.ASSISTANT, ANSWER))
    return (
        ContextEvaluationCase(id="simple", required_messages=required, max_estimated_tokens=300),
        ContextEvaluationCase(
            id="deep-inline",
            required_messages=required,
            required_text=(FACT,),
            max_estimated_tokens=600,
            expected_recovered_messages=recovered,
        ),
        ContextEvaluationCase(
            id="deep-archived",
            required_messages=required,
            required_text=(FACT,),
            max_estimated_tokens=600,
            max_checkpoint_bytes=7000,
            expected_recovered_messages=recovered,
        ),
        ContextEvaluationCase(
            id="working-state-and-skills",
            required_messages=required,
            required_text=("Verify the warranty", GUIDANCE),
            forbidden_text=(UNLOADED_GUIDANCE,),
            max_estimated_tokens=1800,
        ),
    )


async def recover_sources(runtime: AgentRuntime, state: RunState) -> tuple[Message, ...]:
    """Recover archives via the permissioned reader, then append the uncompacted tail."""
    if state.context_compaction is None:
        return tuple(state.context_history or ())
    artifact_id: str | None = state.context_compaction.archive_id
    through = state.context_compaction.through
    chunks: list[list[Message]] = []
    visited: set[str] = set()
    while artifact_id:
        if artifact_id in visited or len(visited) >= 32:
            raise ValueError("invalid archive chain")
        visited.add(artifact_id)
        parts: list[str] = []
        offset = 0
        for _ in range(256):
            result = await runtime.tool_executor.execute(
                "core.context_read",
                {"artifact_id": artifact_id, "offset": offset, "limit": 512},
                ToolContext(
                    run_id=state.id,
                    step_id="evaluation-read",
                    tenant_id=state.request.tenant_id,
                    user_id=state.request.user_id,
                    permissions=frozenset({"context.read"}),
                    idempotency_key=f"evaluation:{artifact_id}:{offset}",
                ),
            )
            page = result.value
            parts.append(page["content"])
            if page["next_offset"] is None:
                break
            if page["next_offset"] <= offset:
                raise ValueError("invalid page cursor")
            offset = page["next_offset"]
        else:
            raise ValueError("archive exceeded evaluation paging limit")
        payload = json.loads("".join(parts))
        if payload["through"] != through:
            raise ValueError("archive chain is not contiguous")
        chunk = [Message.model_validate(item) for item in payload["messages"]]
        if len(chunk) != payload["through"] - payload["start"]:
            raise ValueError("archive source count mismatch")
        chunks.insert(0, chunk)
        through = payload["start"]
        artifact_id = payload["previous_archive_id"]
    if through != 0:
        raise ValueError("archive chain is incomplete")
    tail = (state.context_history or [])[
        state.context_compaction.through - state.context_history_offset :
    ]
    return (*[message for chunk in chunks for message in chunk], *tail)


async def execute(case: ContextEvaluationCase) -> ContextEvaluationArtifact:
    container = build_container(AppSettings())
    runtime = container.runtime
    deep = case.id in {"deep-inline", "deep-archived"}
    structured = case.id == "working-state-and-skills"
    responses = [FACT, ANSWER] if deep else [ANSWER]
    provider = MockModelProvider(
        [
            tool_call_response(
                "core.working_state_update",
                {
                    "expected_revision": 0,
                    "state": {
                        "goal": "Prepare the report",
                        "constraints": [],
                        "decisions": [],
                        "completed_work": [],
                        "open_tasks": ["Verify the warranty"],
                    },
                },
            ),
            tool_call_response("core.skills_load", {"names": ["reporting"]}),
            ANSWER,
        ]
        if structured
        else responses
    )
    runtime.router.register_provider(provider, registration_id="evaluation")
    definition = AgentDefinition(
        name="context-evaluation",
        version="1.0.0",
        description="Offline context regression",
        system_instructions=INSTRUCTIONS,
        default_model=ModelProfile(
            name="offline", provider="evaluation", model="deterministic", context_window_tokens=4096
        ),
        budget=Budget(max_output_tokens=256),
        enabled_tools=frozenset(
            {"core.context_read"}
            if deep
            else {"core.working_state_update", "core.skills_load"}
            if structured
            else ()
        ),
        tool_permissions=frozenset(
            {"context.read"} if deep else {"context.write", "skills.read"} if structured else ()
        ),
        context_policy=ContextPolicy(
            compact_history=deep,
            archive_compacted_history=case.id == "deep-archived",
            compaction_trigger_tokens=256,
            compaction_keep_recent_groups=1,
            compaction_max_output_tokens=128,
            working_state_enabled=structured,
            skills_enabled=structured,
        ),
        skills=(
            SkillDefinition(
                name="reporting",
                version="1.0.0",
                description="Prepare a report",
                instructions=GUIDANCE,
            ),
            SkillDefinition(
                name="editing",
                version="1.0.0",
                description="Edit a report",
                instructions=UNLOADED_GUIDANCE,
            ),
        )
        if structured
        else (),
        approval_policy=ApprovalPolicy(require_for_side_effects=False),
    )
    runtime.agents.register(definition)
    try:
        if deep:
            await runtime.memory.append("example", "evaluation", HISTORY)
        result = await runtime.run(
            RunRequest(
                agent=definition.name,
                input=QUESTION,
                tenant_id="example",
                user_id="operator",
                session_id="evaluation",
            )
        )
        if result.status != RunStatus.COMPLETED:
            raise RuntimeError("evaluation run did not complete")
        checkpoint = await runtime.status(result.run_id, "example")
        # Exercise serialization/rebuild as well as the actual provider-dispatched view.
        restored = RunState.model_validate_json(checkpoint.model_dump_json())
        rebuilt = await runtime.contexts.get(definition.context_builder).build(restored, definition)
        if structured:
            messages = tuple(rebuilt)
        else:
            messages = provider.requests[-1].messages
        return ContextEvaluationArtifact(
            messages=messages,
            checkpoint_bytes=len(checkpoint.model_dump_json().encode("utf-8")),
            recovered_messages=await recover_sources(runtime, restored) if deep else None,
        )
    finally:
        await container.aclose()


async def main() -> None:
    report = await ContextEvaluationRunner().run(cases(), execute)
    print(report.model_dump_json(indent=2))
    if not report.promoted:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
