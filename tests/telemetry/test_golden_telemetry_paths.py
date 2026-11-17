"""Golden telemetry and redaction, continued: conversation turns, workflows, delegated child runs,
provider errors, invalid tool calls, failing policies, credentials the Runtime holds (a caller's,
an env:// secret, a provider key) and the HTTP API's run events.

See golden_harness.py for the canaries, the sinks and how to regenerate the golden files.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from golden_harness import (
    AWS_KEY,
    CALLER_CREDENTIAL,
    CREDENTIAL_ARGUMENT,
    ENV_SECRET,
    HELD_CANARIES,
    OPENAI_KEY,
    PII_CANARIES,
    PROVIDER_API_KEY,
    SECRET_CANARIES,
    TENANT,
    USER,
    WORKING_DATA,
    Telemetry,
    assert_golden,
    assert_no_canary,
    capture_telemetry,
    event_shapes,
    leaky_mapping,
    leaky_text,
)
from golden_scenarios import (
    AGENT,
    CHILD,
    agent_definition,
    collect,
    container,
    request,
    returning,
    settings,
    tool,
    tool_call,
)

from algen_agent_runtime.conversations import ConversationApprovalDecision, MessageStatus
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.tools.contracts import SideEffect
from algen_agent_runtime.types.contracts import (
    FinishReason,
    Message,
    ModelResponse,
    Role,
    RunStatus,
    ToolCall,
)


@pytest.fixture
def telemetry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> Iterator[Telemetry]:
    with capture_telemetry(monkeypatch, caplog, capsys) as captured:
        yield captured


# --- conversation turns -----------------------------------------------------------------------


async def _settled(service: Any, conversation_id: str, *wanted: MessageStatus) -> Any:
    statuses = set(wanted) or {
        MessageStatus.COMPLETED,
        MessageStatus.FAILED,
        MessageStatus.AWAITING_APPROVAL,
    }
    for _ in range(800):
        messages = await service.messages(conversation_id, TENANT)
        if messages and messages[-1].status in statuses:
            return messages[-1]
        await asyncio.sleep(0.005)
    raise AssertionError(f"the turn never reached {statuses}")


async def test_conversation_turn_streams_pauses_and_resumes(telemetry: Telemetry) -> None:
    """A streamed conversation turn: the user's message, a tool result, the answer and the
    approver's rejection reason all carry the canaries."""
    built = container(
        settings(
            agent_definition(enabled_tools=["crm.lookup", "mail.send"]),
            include_content=True,
        ),
        [
            tool("crm.lookup", returning(leaky_mapping("tool result"))),
            tool(
                "mail.send",
                returning({"sent": True}),
                side_effect=SideEffect.EXTERNAL,
                properties={"to": {"type": "string"}, "body": {"type": "string"}},
            ),
        ],
        [
            tool_call("crm.lookup", {"query": "customer 7"}, leaky_text("rationale")),
            tool_call("mail.send", {"to": "ops", "body": "summary"}),
            leaky_text("answer after rejection"),
        ],
    )
    service = built.conversations
    conversation = await service.create(
        tenant_id=TENANT, user_id=USER, agent=AGENT, metadata={"api_key": CREDENTIAL_ARGUMENT}
    )

    await service.submit(
        conversation.id,
        TENANT,
        USER,
        leaky_text("user message"),
        credentials={"authorization": f"Bearer {CALLER_CREDENTIAL}"},
    )
    paused = await _settled(service, conversation.id)
    assert paused.status == MessageStatus.AWAITING_APPROVAL, paused
    await service.respond(
        conversation.id,
        paused.pending.turn_id,
        TENANT,
        USER,
        ConversationApprovalDecision(decision="rejected", reason=leaky_text("reason")[:900]),
        credentials={"authorization": f"Bearer {CALLER_CREDENTIAL}"},
    )
    done = await _settled(service, conversation.id, MessageStatus.COMPLETED, MessageStatus.FAILED)

    conversation_events = list(await service.events.history(conversation.id))
    sinks, shape = await collect(built, telemetry, list(done.run_ids))
    sinks["conversation_events"] = "\n".join(
        event.model_dump_json() for event in conversation_events
    )
    sinks["conversation_messages"] = "\n".join(
        message.model_dump_json() for message in await service.messages(conversation.id, TENANT)
    )
    shape["conversation_events"] = event_shapes(conversation_events)
    # The user's own message is the conversation's history, shown back to them as they wrote
    # it; the approver's note keeps the personal data they wrote (credentials are masked).
    users_own = set(SECRET_CANARIES) | set(PII_CANARIES)
    assert_no_canary(
        sinks,
        scenario="conversation turn",
        allowed={
            **WORKING_DATA,
            "store:conversation_store": users_own,
            "store:conversation_events": users_own,
            "conversation_messages": users_own,
            "conversation_events": users_own,
            "approvals": set(PII_CANARIES),
            "store:approval_store": set(PII_CANARIES),
        },
    )
    # The caller's credential reaches no sink, the conversation's own records included, and
    # a credential in its metadata is masked before it is stored.
    assert_no_canary(sinks, HELD_CANARIES, scenario="conversation turn (held)")
    stored = await service.get(conversation.id, TENANT)
    assert stored.metadata == {"api_key": "[REDACTED]"}
    assert OPENAI_KEY not in stored.title and AWS_KEY not in stored.title
    assert_golden("conversation_turn", shape)


# --- workflows --------------------------------------------------------------------------------


async def test_workflow_agent_tool_handler_and_approval_steps(telemetry: Telemetry) -> None:
    """A workflow whose input, agent step, tool step, approval parameters and failing handler
    carry the canaries."""
    from algen_agent_runtime.workflows import MultiAgentWorkflowExecutor, WorkflowHookRegistry
    from algen_agent_runtime.workflows.contracts import (
        WorkflowApprovalRule,
        WorkflowManifest,
        WorkflowNode,
        WorkflowNodeKind,
    )

    built = container(
        settings(agent_definition(enabled_tools=[])),
        [tool("crm.lookup", returning(leaky_mapping("workflow tool")))],
        [leaky_text("agent step answer")],
    )
    hooks = WorkflowHookRegistry()
    hooks.register_builder("ask", lambda context: {"question": context.state.values["input"]})
    hooks.register_handler(
        "prepare",
        lambda context: {
            "summary": leaky_text("summary"),
            "parameters": {"recipient": "ops@example.org", "api_key": CREDENTIAL_ARGUMENT},
        },
    )

    def explode(context: Any) -> Any:
        raise RuntimeError(leaky_text("handler error"))

    hooks.register_handler("explode", explode)
    manifest = WorkflowManifest(
        name="golden-flow",
        version="1.0.0",
        nodes=(
            WorkflowNode(
                id="ask",
                kind=WorkflowNodeKind.AGENT,
                agent=AGENT,
                input_builder="ask",
                output_key="answer",
            ),
            WorkflowNode(
                id="lookup",
                kind=WorkflowNodeKind.HANDLER,
                handler="tool:crm.lookup",
                arguments={"query": "customer 7"},
                output_key="record",
                depends_on=("ask",),
            ),
            WorkflowNode(
                id="prepare",
                kind=WorkflowNodeKind.HANDLER,
                handler="prepare",
                output_key="proposal",
                depends_on=("lookup",),
            ),
            WorkflowNode(
                id="review",
                kind=WorkflowNodeKind.APPROVAL,
                output_key="approval",
                depends_on=("prepare",),
                approval=WorkflowApprovalRule(
                    prompt="Review the message",
                    review_from=("proposal.summary",),
                    parameters_from="proposal.parameters",
                    parameters_schema={
                        "type": "object",
                        "properties": {
                            "recipient": {"type": "string"},
                            "api_key": {"type": "string"},
                        },
                    },
                ),
            ),
            WorkflowNode(
                id="explode",
                kind=WorkflowNodeKind.HANDLER,
                handler="explode",
                output_key="never",
                depends_on=("review",),
                failure_policy="fail_workflow",
            ),
        ),
    )
    emitted: list[tuple[str, dict[str, Any]]] = []
    executor = MultiAgentWorkflowExecutor(built.runtime, hooks, store=built.workflow_checkpoints)

    paused = await executor.run(
        manifest,
        {"input": leaky_text("workflow input")},
        tenant_id=TENANT,
        user_id=USER,
        emit=lambda event_type, data: emitted.append((event_type, data)),
    )
    assert paused.status.value == "awaiting_approval", paused
    finished = await executor.decide_approval(
        manifest,
        paused.id,
        tenant_id=TENANT,
        user_id=USER,
        decision="approved",
        comment=leaky_text("approver comment"),
        emit=lambda event_type, data: emitted.append((event_type, data)),
    )
    assert finished.status.value == "failed", finished

    run_ids = [
        node.run_ids[0] for node in finished.nodes.values() if getattr(node, "run_ids", None)
    ]
    sinks, shape = await collect(built, telemetry, run_ids)
    sinks["workflow_events"] = json.dumps(emitted, default=str)
    sinks["workflow_state"] = finished.model_dump_json()
    shape["workflow_events"] = [
        {"type": event_type, "payload": event_shapes([{"type": event_type, "data": data}])[0]}
        for event_type, data in emitted
    ]
    # The workflow's own values (its input, what its handlers return, the parameters the
    # approver approved) are its working data, kept in its checkpoint to resume it.
    values = set(SECRET_CANARIES) | set(PII_CANARIES)
    assert_no_canary(
        sinks,
        scenario="workflow",
        allowed={
            **WORKING_DATA,
            "store:workflow_checkpoints": values,
            "workflow_state": values,
        },
    )
    assert_golden("workflow", shape)


# --- delegation -------------------------------------------------------------------------------


async def test_delegated_child_run(telemetry: Telemetry) -> None:
    """A parent delegates to a child whose input, tool result and answer carry the canaries."""
    app = settings(
        agent_definition(
            enabled_tools=["core.subagent", "crm.lookup"],
            tool_permissions=["agents.delegate"],
            delegates=[CHILD],
        ),
        agent_definition(CHILD, enabled_tools=["crm.lookup"]),
        runtime={"recover_incomplete_runs": False, "subagent": {"enabled": True}},
    )
    built = container(
        app,
        [tool("crm.lookup", returning(leaky_mapping("child tool result")))],
        [
            # A delegated task with a credential in it is refused like any tool argument (see
            # test_tool_arguments_with_secrets_are_refused); the child's data carries them.
            tool_call("core.subagent", {"agent": CHILD, "input": "look up customer 7"}),
            tool_call("crm.lookup", {"query": "customer 7"}),
            leaky_text("child answer"),
            leaky_text("parent answer"),
        ],
    )

    result = await built.runtime.run(
        request(leaky_text("input")), credentials={"authorization": CALLER_CREDENTIAL}
    )

    assert result.status == RunStatus.COMPLETED, result.error
    states = await built.runtime.runs.list_active(1000)
    del states
    child_ids = [
        event.data.get("child_run_id")
        for event in await built.runtime.events.history(result.run_id)
        if isinstance(event.data, dict) and event.data.get("child_run_id")
    ]
    sinks, shape = await collect(
        built, telemetry, [result.run_id, *dict.fromkeys(str(item) for item in child_ids)]
    )
    sinks["result"] = result.output or ""
    # The delegated task is the model's own tool-call argument: working data of the parent.
    assert_no_canary(
        sinks,
        scenario="delegation",
        allowed={
            "store:run_store": set(SECRET_CANARIES) | set(PII_CANARIES),
            "store:tool_execution_ledger": set(SECRET_CANARIES) | set(PII_CANARIES),
        },
    )
    assert_golden("delegation", shape)


# --- provider errors --------------------------------------------------------------------------


def _upstream(status: int, body: dict[str, Any]) -> httpx.AsyncClient:
    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(reply))


@pytest.mark.parametrize("stream", [False, True], ids=["generate", "stream"])
async def test_provider_http_error_body(telemetry: Telemetry, stream: bool) -> None:
    """An OpenAI-compatible provider answers 401 with its API key and the canaries in the
    error body."""
    app = settings(
        agent_definition(
            default_model={"name": "up", "provider": "upstream", "model": "up-1"},
        ),
        providers={
            "upstream": {
                "type": "openai_compatible",
                "base_url": "https://llm.example.com/v1",
                "api_key": "env://GOLDEN_PROVIDER_KEY",
                "default_model": "up-1",
                "capabilities": {"streaming": True, "tools": True},
            }
        },
    )
    built = container(app, environment={"GOLDEN_PROVIDER_KEY": PROVIDER_API_KEY})
    provider = built.runtime.router.provider("upstream")
    provider._client = _upstream(
        401,
        {
            "error": {
                "message": f"Incorrect API key provided: {PROVIDER_API_KEY}. {leaky_text('body')}",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        },
    )

    result = await built.runtime.run(request(leaky_text("input"), stream=stream))

    assert result.status == RunStatus.FAILED
    sinks, shape = await collect(built, telemetry, [result.run_id])
    sinks["result"] = str(result.error or "")
    assert_no_canary(sinks, scenario=f"provider error ({'stream' if stream else 'generate'})")
    assert "invalid_api_key" in sinks["result"]
    assert_golden(f"provider_error.{'stream' if stream else 'generate'}", shape)


# --- invalid tool calls and metrics -----------------------------------------------------------


async def test_invalid_tool_call_arguments(telemetry: Telemetry) -> None:
    """The model sends a tool call whose arguments are not a JSON object but carry the
    canaries; the invalid-call metric and the repair request must not repeat them."""
    from algen_agent_runtime.exceptions.errors import InvalidToolCallError

    built = container(
        settings(agent_definition(enabled_tools=["crm.lookup"])),
        [tool("crm.lookup", returning({"ok": True}))],
        ["recovered"],
    )
    model = built.runtime.router.provider("mock")
    assert isinstance(model, MockModelProvider)
    answer = model.generate
    calls: list[Any] = []

    async def garbled(model_request: Any) -> ModelResponse:
        calls.append(model_request)
        if len(calls) == 1:
            raise InvalidToolCallError("mock", "crm.lookup", leaky_text("bad json"))
        return await answer(model_request)

    model.generate = garbled  # type: ignore[method-assign]

    result = await built.runtime.run(request("look it up"))

    sinks, shape = await collect(built, telemetry, [result.run_id])
    sinks["result"] = (result.output or "") + str(result.error or "")
    assert_no_canary(sinks, scenario="invalid tool call")
    assert shape["metrics"], "the invalid tool call metric was recorded"
    assert_golden("invalid_tool_call", shape)


# --- failing policies -------------------------------------------------------------------------


async def test_a_policy_that_raises(telemetry: Telemetry) -> None:
    """A deployment policy raises with the canaries in its message (policy.error audit, metric
    and log)."""

    class Exploding:
        name = "golden.exploding"
        category = "custom"
        enforcement_mode = "enforce"

        async def evaluate(self, point: str, payload: Any, context: Any) -> Any:
            raise RuntimeError(leaky_text(f"policy {point}"))

    built = container(
        settings(
            agent_definition(guardrail_policy={"policies": ["secrets", "pii", "golden.exploding"]}),
            runtime={"recover_incomplete_runs": False, "component_check": "warn"},
        ),
        [],
        [leaky_text("answer")],
    )
    built.runtime.policies.register(Exploding())

    result = await built.runtime.run(request(leaky_text("input")))

    sinks, shape = await collect(built, telemetry, [result.run_id])
    sinks["result"] = (result.output or "") + str(result.error or "")
    assert_no_canary(sinks, scenario="policy error")
    assert_golden("policy_error", shape)


# --- credentials the Runtime holds ------------------------------------------------------------


def _http_server(seen: list[httpx.Request]) -> httpx.AsyncClient:
    """An API that rejects every call, echoing the credential it was sent."""

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        sent = request.headers.get("authorization", "")
        token = sent.split(" ", 1)[-1]
        return httpx.Response(
            401,
            json={
                "error": f"token {token} is expired",
                "received_header": sent,
                "detail": leaky_text("api error"),
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(reply))


@pytest.mark.parametrize("auth", ["env_secret", "caller"])
async def test_http_tool_credentials(
    telemetry: Telemetry, monkeypatch: pytest.MonkeyPatch, auth: str
) -> None:
    """A declared HTTP tool authenticates with an env:// secret or the caller's credential, and
    the API echoes it back in its error body."""
    import algen_agent_runtime.tools.http_api as http_api

    seen: list[httpx.Request] = []

    async def no_dns(url: str, allowed: Any, private: bool) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(http_api, "validate_outbound_url", no_dns)
    monkeypatch.setattr(http_api, "create_safe_http_client", lambda **_: _http_server(seen))
    authentication = (
        {"type": "bearer", "token": "env://GOLDEN_API_TOKEN"}
        if auth == "env_secret"
        else {"type": "caller", "credential": "authorization", "scheme": "Bearer"}
    )
    app = settings(
        agent_definition(enabled_tools=["crm.api"]),
        security={"allowed_http_hosts": ["api.example.com"]},
        http_tools=[
            {
                "name": "crm.api",
                "description": "Read a customer record.",
                "url": "https://api.example.com/customers",
                "auth": authentication,
            }
        ],
    )
    built = container(
        app,
        [],
        [tool_call("crm.api", {}), "the API refused"],
        environment={"GOLDEN_API_TOKEN": ENV_SECRET},
    )

    result = await built.runtime.run(
        request("look it up"), credentials={"authorization": CALLER_CREDENTIAL}
    )

    assert seen, "the tool called the API"
    held = ENV_SECRET if auth == "env_secret" else CALLER_CREDENTIAL
    assert held in seen[0].headers["authorization"]
    sinks, shape = await collect(built, telemetry, [result.run_id])
    sinks["result"] = (result.output or "") + str(result.error or "")
    assert_no_canary(sinks, scenario=f"http tool ({auth})", allowed=WORKING_DATA)
    assert_golden(f"http_tool.{auth}", shape)


# --- the HTTP API -----------------------------------------------------------------------------


async def test_http_api_run_events_and_error_bodies(telemetry: Telemetry) -> None:
    """A run started through the HTTP API with the caller's credential in a credential header;
    its events read over SSE, its state, narrative and approvals over REST."""
    from algen_agent_runtime.api.app import create_app

    app_settings = settings(
        agent_definition(enabled_tools=["crm.lookup", "mail.send"]),
        api={"credential_headers": ["x-upstream-token"]},
    )
    built = container(
        app_settings,
        [
            tool("crm.lookup", returning(leaky_mapping("tool result"))),
            tool(
                "mail.send",
                returning({"sent": True}),
                side_effect=SideEffect.EXTERNAL,
                properties={"to": {"type": "string"}, "api_key": {"type": "string"}},
            ),
        ],
        [
            tool_call("crm.lookup", {"query": "customer 7"}),
            tool_call("mail.send", {"to": "ops", "api_key": CREDENTIAL_ARGUMENT}),
            leaky_text("answer"),
        ],
    )
    headers = {
        "x-tenant-id": TENANT,
        "x-user-id": USER,
        "x-scopes": "runs:read runs:write approvals:read approvals:decide",
        "x-upstream-token": CALLER_CREDENTIAL,
    }
    transport = httpx.ASGITransport(app=create_app(app_settings, built))
    bodies: list[str] = []
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/runs",
            headers=headers,
            json={"agent": AGENT, "input": leaky_text("input"), "stream": True},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        for _ in range(400):
            state = await client.get(f"/v1/runs/{run_id}", headers=headers)
            if state.json()["status"] in {"awaiting_approval", "completed", "failed"}:
                break
            await asyncio.sleep(0.005)
        assert state.json()["status"] == "awaiting_approval", state.text
        bodies.append((await client.get(f"/v1/runs/{run_id}/events", headers=headers)).text)
        bodies.append((await client.get("/v1/approvals", headers=headers)).text)
        refused = await client.post(
            f"/v1/runs/{run_id}/approval",
            headers=headers,
            json={"decision": "maybe", "comment": leaky_text("bad decision")},
        )
        bodies.append(refused.text)
        decided = await client.post(
            f"/v1/runs/{run_id}/approval",
            headers=headers,
            json={"decision": "approved", "comment": leaky_text("comment")},
        )
        assert decided.status_code == 202, decided.text
        for _ in range(400):
            state = await client.get(f"/v1/runs/{run_id}", headers=headers)
            if state.json()["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(0.005)
        bodies.append(state.text)
        bodies.append(
            (
                await client.get(f"/v1/runs/{run_id}/events?through_pauses=true", headers=headers)
            ).text
        )
        bodies.append((await client.get(f"/v1/runs/{run_id}/narrative", headers=headers)).text)

    sinks, shape = await collect(built, telemetry, [run_id])
    sinks["http_sse"] = bodies[0] + bodies[4]
    sinks["http_bodies"] = "\n".join(bodies[1:4] + bodies[5:])
    # The approver's note keeps the personal data they wrote (credentials are masked); the
    # approval inbox shows it back.
    note = set(PII_CANARIES)
    working = {"credential_argument"}
    assert_no_canary(
        sinks,
        scenario="http api",
        allowed={
            **WORKING_DATA,
            "store:run_store": working,
            "store:tool_execution_ledger": working,
            "store:approval_store": working | note,
            "approvals": working | note,
            # The run's own state (GET /v1/runs/{id}) returns its messages: the run's working
            # data, to the user who started it.
            "http_bodies": working | note,
        },
    )
    assert "event: run.completed" in sinks["http_sse"]
    assert_golden("http_api", shape)


def test_canary_values_are_distinct() -> None:
    values = [*SECRET_CANARIES.values(), *PII_CANARIES.values(), *HELD_CANARIES.values()]
    assert len(values) == len(set(values))
    assert ToolCall and Message and Role and FinishReason  # re-exported for scenario authors
