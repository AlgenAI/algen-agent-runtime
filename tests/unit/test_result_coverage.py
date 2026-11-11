"""A tool result can say what it covered, and an answer stating absence on a partial search is
flagged (#135)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import make_agent, make_runtime
from pydantic import ValidationError

from algen_agent_runtime.context.artifacts import ContextArtifactService
from algen_agent_runtime.context.packing import REPAIR_MESSAGE_NAME
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.tools.contracts import ResultCoverage, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.types.contracts import (
    ContextPolicy,
    Message,
    Role,
    RunRequest,
    RunState,
    RunStatus,
    ToolCall,
    VerificationPolicy,
    utc_now,
)
from algen_agent_runtime.verification.coverage import ResultCoverageVerifier
from algen_agent_runtime.verification.verifiers import VerificationService

DERIVED_EMPTY = ResultCoverage(
    searched="mail.attachments_index", filters={"message_id": "m-1"}, returned=0, source="derived"
)
CAPPED = ResultCoverage(
    searched="hr.applications", total=1_200, returned=50, capped=True, source="system_of_record"
)
COMPLETE = ResultCoverage(
    searched="hr.applications", total=0, returned=0, source="system_of_record"
)


# The contract


def test_complete_only_for_an_uncapped_system_of_record_search() -> None:
    assert COMPLETE.complete
    assert not DERIVED_EMPTY.complete
    assert not CAPPED.complete
    short = ResultCoverage(searched="x", total=10, returned=3, source="system_of_record")
    assert not short.complete


def test_attach_and_read_back_from_a_value_its_json_or_a_wrapped_result() -> None:
    result = CAPPED.attach({"items": []})
    assert result["coverage"] == {
        "searched": "hr.applications",
        "filters": {},
        "total": 1200,
        "returned": 50,
        "capped": True,
        "source": "system_of_record",
    }
    assert ResultCoverage.of(result) == CAPPED
    assert ResultCoverage.of(json.dumps(result)) == CAPPED
    wrapped = {"untrusted_content_warning": "...", "result": result}
    assert ResultCoverage.of(json.dumps(wrapped)) == CAPPED


@pytest.mark.parametrize(
    "result",
    [
        None,
        "not json",
        [1, 2],
        {"items": []},
        {"coverage": "all of it"},
        {"coverage": {"searched": "x", "returned": -1, "source": "derived"}},
        {"coverage": {"searched": "x", "returned": 0, "source": "guess"}},
    ],
)
def test_missing_or_malformed_coverage_reads_as_none(result: Any) -> None:
    assert ResultCoverage.of(result) is None


def test_unknown_coverage_keys_are_ignored() -> None:
    payload = {**CAPPED.model_dump(), "as_of": "2026-11-01"}
    assert ResultCoverage.of({"coverage": payload}) == CAPPED


def test_filters_are_bounded_json() -> None:
    with pytest.raises(ValidationError):
        ResultCoverage(searched="x", returned=0, source="derived", filters={"q": "y" * 3_000})
    with pytest.raises(ValidationError):
        ResultCoverage(searched="x", returned=0, source="derived", filters={"q": object()})


# The verifier


def state_with(*results: tuple[str, Any], history: list[Message] | None = None) -> RunState:
    state = RunState(
        request=RunRequest(agent="a", input="any attachment?", tenant_id="t", user_id="u"),
        session_id="s",
    )
    messages = [*(history or []), Message.text(Role.USER, "any attachment?")]
    for index, (tool, value) in enumerate(results):
        call = ToolCall(id=f"c{index}", name=tool, arguments={})
        messages.append(Message(role=Role.ASSISTANT, content=(), tool_calls=(call,)))
        messages.append(
            Message.text(Role.TOOL, json.dumps(value)).model_copy(
                update={"tool_call_id": call.id, "name": tool}
            )
        )
    state.context_history = messages
    return state


async def verify(answer: str, state: RunState) -> Any:
    return await ResultCoverageVerifier().verify(answer, {"state": state})


async def test_tools_without_coverage_pass_as_before() -> None:
    result = await verify("There is no attachment.", state_with(("mail.search", {"items": []})))
    assert result.passed and result.reason_code == "coverage.not_reported"


async def test_an_absence_claim_on_a_derived_empty_result_fails_with_findings() -> None:
    state = state_with(("mail.attachments", DERIVED_EMPTY.attach({"items": []})))
    result = await verify("This email has no attachment.", state)
    assert not result.passed
    assert result.reason_code == "coverage.partial_absence"
    assert result.findings == (
        "mail.attachments searched mail.attachments_index: "
        "read a derived store, not the system of record",
    )
    assert result.repair_hint
    # Filter values can hold user data; they never appear.
    assert "m-1" not in result.message


async def test_an_absence_claim_on_a_capped_result_fails() -> None:
    state = state_with(("hr.search", CAPPED.attach({"items": []})))
    result = await verify("None of the applicants is from Pune.", state)
    assert not result.passed
    assert result.findings == ("hr.search searched hr.applications: capped at 50 of 1200 results",)


@pytest.mark.parametrize(
    "answer",
    [
        "I found 3 attachments: a.pdf, b.pdf and c.pdf.",
        "The email was sent on Monday.",
    ],
)
async def test_an_answer_that_does_not_state_absence_passes(answer: str) -> None:
    state = state_with(("mail.attachments", DERIVED_EMPTY.attach({"items": []})))
    result = await verify(answer, state)
    assert result.passed and result.reason_code == "coverage.no_absence_claim"


@pytest.mark.parametrize(
    "answer",
    [
        "The index shows no attachment, but it may be incomplete.",
        "I could not find an attachment in the search index; I can't confirm none exists.",
        "No match in the first 50 results only; the list is capped.",
    ],
)
async def test_a_disclosed_partial_search_passes(answer: str) -> None:
    state = state_with(("mail.attachments", DERIVED_EMPTY.attach({"items": []})))
    result = await verify(answer, state)
    assert result.passed and result.reason_code == "coverage.disclosed"


async def test_a_complete_search_of_the_same_collection_backs_the_claim() -> None:
    state = state_with(
        ("hr.search", CAPPED.attach({"items": []})),
        ("hr.search", COMPLETE.attach({"items": []})),
    )
    result = await verify("No application is on record.", state)
    assert result.passed and result.reason_code == "coverage.complete"


async def test_a_complete_search_of_another_collection_does_not() -> None:
    other = COMPLETE.model_copy(update={"searched": "hr.employees"})
    state = state_with(
        ("mail.attachments", DERIVED_EMPTY.attach({"items": []})),
        ("hr.lookup", other.attach({"items": []})),
    )
    assert not (await verify("There is no attachment.", state)).passed


async def test_only_the_current_runs_results_count() -> None:
    earlier = state_with(("mail.attachments", DERIVED_EMPTY.attach({"items": []})))
    assert earlier.context_history is not None
    later = state_with(history=earlier.context_history)
    result = await verify("There is no attachment.", later)
    assert result.passed and result.reason_code == "coverage.not_reported"


async def test_results_fetched_after_a_repair_turn_count() -> None:
    state = state_with(("mail.attachments", DERIVED_EMPTY.attach({"items": []})))
    assert state.context_history is not None
    state.context_history.append(
        Message.text(Role.USER, "repair").model_copy(update={"name": REPAIR_MESSAGE_NAME})
    )
    call = ToolCall(id="again", name="mail.fetch", arguments={})
    state.context_history += [
        Message(role=Role.ASSISTANT, content=(), tool_calls=(call,)),
        Message.text(
            Role.TOOL,
            json.dumps(
                ResultCoverage(
                    searched="mail.attachments_index", returned=0, source="system_of_record"
                ).attach({})
            ),
        ).model_copy(update={"tool_call_id": "again", "name": "mail.fetch"}),
    ]
    result = await verify("This email has no attachment.", state)
    assert result.passed and result.reason_code == "coverage.complete"


async def test_patterns_are_configurable() -> None:
    state = state_with(("mail.attachments", DERIVED_EMPTY.attach({"items": []})))
    verifier = ResultCoverageVerifier(absence_patterns=[r"\bkein\b"], disclosure_patterns=[])
    assert (await verifier.verify("There is no attachment.", {"state": state})).passed
    assert not (await verifier.verify("Kein Anhang.", {"state": state})).passed


def test_registered_by_default_and_off_until_named() -> None:
    assert "result_coverage" in VerificationService().names()
    assert "result_coverage" not in VerificationPolicy().verifiers


# Through a run


def attachments_tool(coverage: ResultCoverage) -> Tool:
    definition = ToolDefinition(
        name="mail.attachments",
        version="1",
        description="List an email's attachments from the search index.",
        input_schema={"type": "object", "properties": {"message_id": {"type": "string"}}},
        output_schema={"type": "object"},
    )

    async def handler(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        return coverage.attach({"items": []})

    return Tool(definition, handler)


async def test_a_run_repairs_an_undisclosed_absence_claim() -> None:
    provider = MockModelProvider(
        [
            tool_call_response("mail.attachments", {"message_id": "m-1"}),
            "This email has no attachment.",
            "The search index shows no attachment for this email, but the index may be "
            "incomplete, so I can't confirm none exists.",
        ]
    )
    runtime = make_runtime(
        provider,
        make_agent(
            enabled_tools=frozenset({"mail.attachments"}),
            verification_policy=VerificationPolicy(
                verifiers=("non_empty", "result_coverage"), max_repairs=1
            ),
        ),
    )
    runtime.tools.register(attachments_tool(DERIVED_EMPTY))

    result = await runtime.run(
        RunRequest(agent="test-agent", input="any attachment?", tenant_id="t", user_id="u")
    )

    assert result.status == RunStatus.COMPLETED
    assert "may be" in (result.output or "")
    repair = provider.requests[-1].messages[-1]
    assert repair.name == REPAIR_MESSAGE_NAME
    assert "read a derived store" in repair.text_content
    events = await runtime.events.history(result.run_id)
    checks = [
        check["reason_code"]
        for event in events
        if event.type == "verification.completed"
        for check in event.data["checks"]
    ]
    assert "coverage.partial_absence" in checks and "coverage.disclosed" in checks


async def test_offloading_keeps_the_coverage_visible() -> None:
    service = ContextArtifactService(InMemoryArtifactStore())
    text = json.dumps(CAPPED.attach({"items": ["row " * 50] * 40}))
    context = ToolContext(
        run_id="run",
        step_id="step",
        tenant_id="tenant",
        user_id="user",
        permissions=frozenset(),
        idempotency_key="run:call",
    )
    policy = ContextPolicy(offload_tool_results=True, tool_result_max_inline_bytes=1024)
    reference, artifact_id = await service.offload(text, context, policy, started_at=utc_now())
    assert artifact_id is not None
    assert ResultCoverage.of(reference) == CAPPED
