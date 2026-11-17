"""Golden telemetry and redaction, continued: what a user adds to a running or paused run
(steering, ``core.ask_user`` and clarification answers), learned memory, files given to a run
(upload metadata, names, scans, errors), the tool-result cache and an MCP server on OAuth whose
tool echoes the token it was sent.

See golden_harness.py for the canaries, the sinks and how to regenerate the golden files.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from golden_harness import (
    CREDENTIAL_ARGUMENT,
    EMAIL,
    HELD_CANARIES,
    MCP_OAUTH_TOKEN,
    PHONE,
    PII_CANARIES,
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
    agent_definition,
    collect,
    container,
    request,
    returning,
    settings,
    tool,
    tool_call,
)

from algen_agent_runtime.conversations import ConversationInputAnswer, MessageStatus
from algen_agent_runtime.memory.service import MEMORY_RECALL, MEMORY_WRITE
from algen_agent_runtime.tools.ask_user import ASK_USER_PERMISSION, ASK_USER_TOOL
from algen_agent_runtime.tools.contracts import ToolContext
from algen_agent_runtime.types.contracts import RunStatus


@pytest.fixture
def telemetry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> Iterator[Telemetry]:
    with capture_telemetry(monkeypatch, caplog, capsys) as captured:
        yield captured


CONTENT = pytest.mark.parametrize(
    "include_content", [False, True], ids=["content-off", "content-on"]
)


def _name(base: str, include_content: bool) -> str:
    return f"{base}.{'content_on' if include_content else 'content_off'}"


CLARIFYING = "golden-clarifying"


def _asking_agent(**updates: Any) -> dict[str, Any]:
    return agent_definition(
        enabled_tools=[ASK_USER_TOOL, "crm.lookup"],
        tool_permissions=[ASK_USER_PERMISSION],
        **updates,
    )


async def _until(runtime: Any, run_id: str, *statuses: RunStatus) -> Any:
    for _ in range(800):
        state = await runtime.status(run_id, TENANT)
        if state.status in statuses:
            return state
        await asyncio.sleep(0.005)
    raise AssertionError(f"the run never reached {statuses}")


# --- steering, core.ask_user and clarification answers ------------------------------------


@CONTENT
async def test_steering_and_answers_to_a_paused_run(
    telemetry: Telemetry, include_content: bool
) -> None:
    """The model asks the user (``core.ask_user``); the user steers the paused run and answers;
    a second run is answered as a plain clarification; a third is steered and cancelled, so its
    steering is never delivered. Every text the user adds carries the canaries."""
    built = container(
        settings(
            _asking_agent(),
            agent_definition(CLARIFYING, planning_strategy="rule_based"),
            include_content=include_content,
        ),
        [tool("crm.lookup", returning({"ok": True}))],
        [
            tool_call(ASK_USER_TOOL, {"question": "Which account?"}),
            leaky_text("answer after steering"),
            leaky_text("answer after clarification"),
            tool_call(ASK_USER_TOOL, {"question": "Which team?"}),
        ],
    )
    runtime = built.runtime

    asked = await runtime.run(request("check the account"))
    assert asked.status == RunStatus.AWAITING_CLARIFICATION, asked.error
    await runtime.steer(asked.run_id, TENANT, leaky_text("steer while paused"))
    await runtime.resume(asked.run_id, TENANT, {"clarification": leaky_text("ask_user answer")})
    answered = await runtime.wait(asked.run_id, TENANT)
    assert answered.status == RunStatus.COMPLETED, answered.error

    # A clarification the planner asks for (an empty request to the rule_based planner) is
    # answered as a user message.
    plain = await runtime.run(request(" ", agent=CLARIFYING))
    assert plain.status == RunStatus.AWAITING_CLARIFICATION, plain.error
    await runtime.resume(plain.run_id, TENANT, {"clarification": leaky_text("clarification")})
    clarified = await runtime.wait(plain.run_id, TENANT)
    assert clarified.status == RunStatus.COMPLETED, clarified.error

    left = await runtime.run(request("check the team"))
    assert left.status == RunStatus.AWAITING_CLARIFICATION, left.error
    await runtime.steer(left.run_id, TENANT, leaky_text("steer never delivered"))
    await runtime.cancel(left.run_id, TENANT)
    cancelled = await runtime.wait(left.run_id, TENANT)
    assert cancelled.status == RunStatus.CANCELLED

    run_ids = [asked.run_id, plain.run_id, left.run_id]
    sinks, shape = await collect(built, telemetry, run_ids)
    sinks["results"] = "\n".join(
        result.model_dump_json() for result in (answered, clarified, cancelled)
    )
    assert_no_canary(sinks, scenario="steering and answers", allowed=WORKING_DATA)
    assert_golden(_name("steering_and_answers", include_content), shape)


async def _settled(service: Any, conversation_id: str, *wanted: MessageStatus) -> Any:
    statuses = set(wanted) or {
        MessageStatus.COMPLETED,
        MessageStatus.FAILED,
        MessageStatus.AWAITING_INPUT,
    }
    for _ in range(800):
        messages = await service.messages(conversation_id, TENANT)
        if messages and messages[-1].status in statuses:
            return messages[-1]
        await asyncio.sleep(0.005)
    raise AssertionError(f"the turn never reached {statuses}")


async def test_conversation_turn_answer_and_steering(telemetry: Telemetry) -> None:
    """A conversation turn the model pauses with ``core.ask_user``: the user steers it and
    answers it through the conversation."""
    built = container(
        settings(_asking_agent(), include_content=True),
        [tool("crm.lookup", returning({"ok": True}))],
        [
            tool_call(ASK_USER_TOOL, {"question": "Which account?"}),
            leaky_text("answer"),
        ],
    )
    service = built.conversations
    conversation = await service.create(tenant_id=TENANT, user_id=USER, agent=AGENT)
    await service.submit(conversation.id, TENANT, USER, "check the account")
    paused = await _settled(service, conversation.id)
    assert paused.status == MessageStatus.AWAITING_INPUT, paused
    await service.steer(
        conversation.id, paused.pending.turn_id, TENANT, USER, leaky_text("conversation steer")
    )
    await service.respond(
        conversation.id,
        paused.pending.turn_id,
        TENANT,
        USER,
        ConversationInputAnswer(text=leaky_text("conversation answer")),
    )
    done = await _settled(service, conversation.id, MessageStatus.COMPLETED, MessageStatus.FAILED)
    assert done.status == MessageStatus.COMPLETED, done

    conversation_events = list(await service.events.history(conversation.id))
    sinks, shape = await collect(built, telemetry, list(done.run_ids))
    sinks["conversation_events"] = "\n".join(
        event.model_dump_json() for event in conversation_events
    )
    sinks["conversation_messages"] = "\n".join(
        message.model_dump_json() for message in await service.messages(conversation.id, TENANT)
    )
    shape["conversation_events"] = event_shapes(conversation_events)
    # What the user wrote is the conversation's history, shown back to them (see the
    # conversation_turn scenario); every telemetry sink still masks it.
    users_own = set(SECRET_CANARIES) | set(PII_CANARIES)
    assert_no_canary(
        sinks,
        scenario="conversation answer and steering",
        allowed={
            **WORKING_DATA,
            "store:conversation_store": users_own,
            "store:conversation_events": users_own,
            "conversation_messages": users_own,
            "conversation_events": users_own,
        },
    )
    assert_golden("conversation_answer_and_steering", shape)


# --- learned memory ---------------------------------------------------------------------------


@CONTENT
async def test_learned_memory_write_and_recall(telemetry: Telemetry, include_content: bool) -> None:
    """The model writes a memory carrying personal data (a credential in the arguments is
    refused before the write) and recalls it."""
    built = container(
        settings(
            agent_definition(
                enabled_tools=[MEMORY_WRITE, MEMORY_RECALL],
                tool_permissions=["memory.write", "memory.read"],
                learned_memory_policy={"enabled": True},
                approval_policy={"require_for_side_effects": False},
                tool_policy={"on_error": "return_to_model"},
                max_steps=20,
            ),
            include_content=include_content,
        ),
        [],
        [
            tool_call(
                MEMORY_WRITE,
                {
                    "key": "contact",
                    "expected_revision": 0,
                    "kind": "fact",
                    "content": leaky_text("memory content"),
                    "evidence": leaky_text("memory evidence"),
                },
            ),
            tool_call(
                MEMORY_WRITE,
                {
                    "key": "contact",
                    "expected_revision": 0,
                    "kind": "fact",
                    "content": f"Reach the customer at {EMAIL} or phone {PHONE}.",
                    "evidence": f"The customer said so ({EMAIL}).",
                },
            ),
            tool_call(MEMORY_RECALL, {"key": "contact"}),
            tool_call(MEMORY_RECALL, {"query": "customer"}),
            leaky_text("answer"),
        ],
    )
    result = await built.runtime.run(request(leaky_text("remember this")))
    assert result.status == RunStatus.COMPLETED, result.error

    sinks, shape = await collect(built, telemetry, [result.run_id])
    sinks["result"] = (result.output or "") + str(result.error or "")
    # The model's own reply (its call, as it wrote it) stays in the run's messages, as in the
    # refused tool arguments scenario; the learned memory store keeps no credential and the
    # personal data only masked.
    assert_no_canary(
        sinks,
        scenario="learned memory",
        allowed={"store:run_store": set(SECRET_CANARIES) | set(PII_CANARIES)},
    )
    assert_golden(_name("learned_memory", include_content), shape)


# --- the tool-result cache --------------------------------------------------------------------


async def test_tool_result_cache_hits(telemetry: Telemetry) -> None:
    """Two runs make the same cacheable call: the second is served from the cache."""
    calls: list[str] = []

    async def lookup(arguments: dict[str, Any], context: ToolContext) -> Any:
        calls.append(arguments["query"])
        return leaky_mapping("cached tool result")

    built = container(
        settings(
            agent_definition(enabled_tools=["crm.lookup"]),
            cache={"backend": "memory", "policies": {"tool_results": {"enabled": True}}},
        ),
        [tool("crm.lookup", lookup)],
        [
            tool_call("crm.lookup", {"query": "customer 7"}),
            leaky_text("first answer"),
            tool_call("crm.lookup", {"query": "customer 7"}),
            leaky_text("second answer"),
        ],
    )
    first = await built.runtime.run(request(leaky_text("first")))
    second = await built.runtime.run(request(leaky_text("second")))
    assert first.status == second.status == RunStatus.COMPLETED, (first.error, second.error)
    assert len(calls) == 1, "the second call is a cache hit"

    sinks, shape = await collect(built, telemetry, [first.run_id, second.run_id])
    assert any(
        name == "algen_agent_runtime.cache.operations" and attributes.get("cache.outcome") == "hit"
        for name, attributes in telemetry.metric_points()
    )
    # The cache holds the tool's result as the ledger does (the run's working data).
    assert_no_canary(
        sinks,
        scenario="tool result cache",
        allowed={**WORKING_DATA, "store:cache": {"credential_argument"}},
    )
    assert_golden("tool_result_cache", shape)


# --- files given to a run ---------------------------------------------------------------------


async def test_uploaded_files_and_attachments(telemetry: Telemetry) -> None:
    """Files uploaded over HTTP with personal data in their names and credentials in their
    metadata; one is given to a run and read by a tool, one is quarantined by the scanner, and
    the refused uploads and attachments answer with errors."""
    from algen_agent_runtime.api.app import create_app

    app_settings = settings(
        agent_definition(
            enabled_tools=["files.read"],
            attachments={"enabled": True, "tool_read_types": ["text/csv"]},
        ),
        security={"upload_scanner": "basic"},
    )

    async def read_file(arguments: dict[str, Any], context: ToolContext) -> Any:
        attachment = await context.read_attachment(arguments["query"])
        return {"rows": attachment.data.decode()[:200]}

    built = container(
        app_settings,
        [tool("files.read", read_file)],
        [],
    )
    headers = {
        "x-tenant-id": TENANT,
        "x-user-id": USER,
        "x-scopes": "runs:read runs:write artifacts:read artifacts:write",
    }
    secret_name = f"contacts-{EMAIL}-{PHONE}.csv"
    metadata = json.dumps({"note": leaky_text("upload metadata"), "api_key": CREDENTIAL_ARGUMENT})
    transport = httpx.ASGITransport(app=create_app(app_settings, built))
    bodies: list[str] = []
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        uploaded = await client.post(
            "/v1/artifacts",
            params={"name": secret_name, "media_type": "text/csv"},
            headers={**headers, "x-artifact-metadata": metadata},
            content=leaky_text("file content").encode(),
        )
        assert uploaded.status_code == 201, uploaded.text
        artifact_id = uploaded.json()["id"]
        bodies.append(uploaded.text)
        quarantined = await client.post(
            "/v1/artifacts",
            params={"name": f"scan-{EMAIL}.png", "media_type": "image/png"},
            headers={**headers, "x-artifact-metadata": metadata},
            content=leaky_text("not an image").encode(),
        )
        assert quarantined.json()["status"] == "quarantined", quarantined.text
        bodies.append(quarantined.text)
        refused = await client.post(
            "/v1/artifacts",
            params={"name": secret_name},
            headers={**headers, "x-artifact-metadata": leaky_text("not json")},
            content=b"x",
        )
        assert refused.status_code == 422
        bodies.append(refused.text)
        model = built.runtime.router.provider("mock")
        model.queue(  # type: ignore[attr-defined]
            tool_call("files.read", {"query": artifact_id}), leaky_text("answer")
        )
        created = await client.post(
            "/v1/runs",
            headers=headers,
            json={
                "agent": AGENT,
                "input": leaky_text("read my file"),
                "attachments": [{"artifact_id": artifact_id}],
            },
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        for _ in range(400):
            state = await client.get(f"/v1/runs/{run_id}", headers=headers)
            if state.json()["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(0.005)
        assert state.json()["status"] == "completed", state.text
        missing = await client.post(
            "/v1/runs",
            headers=headers,
            json={
                "agent": AGENT,
                "input": "read it",
                "attachments": [{"artifact_id": f"missing-{EMAIL}"}],
            },
        )
        assert missing.status_code >= 400
        bodies.append(missing.text)
        refused_file = await client.post(
            "/v1/runs",
            headers=headers,
            json={
                "agent": AGENT,
                "input": "read it",
                "attachments": [{"artifact_id": quarantined.json()["id"]}],
            },
        )
        # Accepted, then failed by the run: the file is quarantined.
        assert refused_file.status_code == 202, refused_file.text
        refused_run = refused_file.json()["run_id"]
        for _ in range(400):
            failed = await client.get(f"/v1/runs/{refused_run}", headers=headers)
            if failed.json()["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(0.005)
        assert failed.json()["status"] == "failed", failed.text
        bodies.append(failed.text)
        bodies.append((await client.get("/v1/artifacts", headers=headers)).text)
        bodies.append(
            (await client.get(f"/v1/artifacts/{artifact_id}/metadata", headers=headers)).text
        )
        bodies.append((await client.get(f"/v1/runs/{run_id}/events", headers=headers)).text)

    sinks, shape = await collect(built, telemetry, [run_id, refused_run])
    sinks["http_bodies"] = "\n".join(bodies)
    # The test's own HTTP client logs the URLs it requests (the file's name is a query
    # parameter); that is the client's log, not the Runtime's.
    sinks["logs"] = "\n".join(
        line for line in sinks["logs"].splitlines() if "HTTP Request:" not in line
    )
    # The user's file, its name and its metadata are their own record, returned to them; the
    # run reads the file through its tool (the run's working data). Telemetry carries none of it.
    users_own = set(SECRET_CANARIES) | set(PII_CANARIES)
    assert_no_canary(
        sinks,
        scenario="uploads and attachments",
        allowed={
            "store:artifact_store": users_own,
            "store:run_store": users_own,
            "store:tool_execution_ledger": {"credential_argument"},
            # The file's name and metadata note keep the personal data the user wrote;
            # credentials in its metadata are masked when it is uploaded.
            "http_bodies": set(PII_CANARIES),
        },
    )
    assert_golden("uploads_and_attachments", shape)


# --- MCP on OAuth -----------------------------------------------------------------------------


class EchoingSession:
    """An MCP session that sends each call over HTTP with the caller's stored token (as the
    real transport does) and whose server echoes the token back: in the result of ``echo`` and
    in the error of ``fail``."""

    def __init__(self, auth: Any) -> None:
        self.auth = auth

    async def initialize(self) -> None:
        pass

    async def list_tools(self, cursor: str | None = None) -> Any:
        schema = {"type": "object", "properties": {"text": {"type": "string"}}}
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name=name,
                    description=f"{name} a message",
                    inputSchema=schema,
                    outputSchema=None,
                    annotations=SimpleNamespace(readOnlyHint=True, destructiveHint=False),
                )
                for name in ("echo", "fail", "refuse")
            ]
        )

    async def _sent_authorization(self) -> str:
        flow = self.auth.async_auth_flow(httpx.Request("POST", "https://mcp.example.com/mcp"))
        sent = await flow.__anext__()
        await flow.aclose()
        return str(sent.headers.get("authorization", ""))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        authorization = await self._sent_authorization()
        assert authorization == f"Bearer {MCP_OAUTH_TOKEN}"
        # The server quotes the token without its scheme, so no shape rule recognises it.
        token = authorization.removeprefix("Bearer ")
        if name == "fail":
            raise RuntimeError(f"upstream rejected token {token} for {EMAIL}")
        text = f"server saw token {token} for {arguments.get('text')}"
        return SimpleNamespace(
            isError=name == "refuse",
            content=[SimpleNamespace(text=text)],
            structuredContent={"seen": {"token": token, "as": token}} if name == "echo" else None,
        )


async def _oauth_mcp_tools() -> list[Any]:
    from algen_agent_runtime.mcp.client import MCPClientManager
    from algen_agent_runtime.mcp.contracts import MCPStreamableHttpServerConfig, OAuthClientAuth
    from algen_agent_runtime.mcp.oauth import MCPOAuthManager
    from algen_agent_runtime.mcp.tokens import InMemoryMCPTokenStore, MCPOAuthTokenSet

    environment = {"GOLDEN_MCP_CLIENT_SECRET": "golden-mcp-client-secret"}
    config = MCPStreamableHttpServerConfig(
        name="golden",
        url="https://mcp.example.com/mcp",
        auth=OAuthClientAuth(
            client_id="golden-client",
            client_secret_ref="env://GOLDEN_MCP_CLIENT_SECRET",
            issuer="https://auth.example.com",
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
            scopes=("messages:write",),
            redirect_uri="https://runtime.example.com/v1/mcp/oauth/callback",
        ),
    )
    store = InMemoryMCPTokenStore()
    manager = MCPOAuthManager(
        [config],
        store,
        environment=environment,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    await store.put(
        manager.key_for("golden", TENANT, USER),
        MCPOAuthTokenSet(
            access_token=MCP_OAUTH_TOKEN,
            refresh_token=MCP_OAUTH_TOKEN[::-1],
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            connected_at=datetime.now(UTC),
            connected_by=USER,
        ),
    )

    async def session_factory(config: Any, stack: Any, *, auth: Any) -> EchoingSession:
        return EchoingSession(auth)

    mcp = MCPClientManager(
        [config], environment=environment, session_factory=session_factory, oauth=manager
    )
    return list(await mcp.discover_tools("golden", tenant_id=TENANT, user_id=USER))


@CONTENT
async def test_mcp_oauth_token_echoed_by_a_tool(
    telemetry: Telemetry, include_content: bool
) -> None:
    """The MCP server echoes the caller's OAuth access token in a result, a structured result,
    a reported error and a raised error: the token reaches no sink, the run's own records
    included."""
    built = container(
        settings(
            agent_definition(
                enabled_tools=["mcp.golden.echo", "mcp.golden.fail", "mcp.golden.refuse"],
                tool_permissions=["mcp.golden"],
                approval_policy={"require_for_side_effects": False},
                tool_policy={"on_error": "return_to_model"},
            ),
            include_content=include_content,
        ),
        await _oauth_mcp_tools(),
        [
            tool_call("mcp.golden.echo", {"text": "hello"}),
            tool_call("mcp.golden.refuse", {"text": "hello"}),
            tool_call("mcp.golden.fail", {"text": "hello"}),
            "done",
        ],
    )
    result = await built.runtime.run(request("post hello"))
    assert result.status == RunStatus.COMPLETED, result.error

    sinks, shape = await collect(built, telemetry, [result.run_id])
    sinks["result"] = result.model_dump_json()
    assert_no_canary(sinks, HELD_CANARIES, scenario="mcp oauth token")
    assert_no_canary(
        {k: v for k, v in sinks.items() if not k.startswith("store:")},
        PII_CANARIES,
        scenario="mcp oauth error",
    )
    assert_golden(_name("mcp_oauth_tool", include_content), shape)


# --- errors that quote the values they reject -------------------------------------------------


@pytest.mark.parametrize("on_error", ["return_to_model", "fail_run"])
async def test_schema_errors_quoting_values(telemetry: Telemetry, on_error: str) -> None:
    """JSON Schema errors quote the value they reject: arguments the model wrote that do not
    fit the tool's input schema, and a tool result that does not fit its output schema."""
    from algen_agent_runtime.tools.contracts import Tool, ToolDefinition

    strict = Tool(
        ToolDefinition(
            name="crm.count",
            version="1",
            description="crm.count for the golden suite.",
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
            },
            output_schema={"type": "object", "properties": {"total": {"type": "integer"}}},
        ),
        returning({"total": f"total for {EMAIL} phone {PHONE}"}),
    )
    built = container(
        settings(agent_definition(enabled_tools=["crm.count"], tool_policy={"on_error": on_error})),
        [strict],
        [
            tool_call("crm.count", {"count": f"customer {EMAIL} phone {PHONE}"}),
            tool_call("crm.count", {"count": 7}),
            "done",
        ],
    )
    result = await built.runtime.run(request("count them"))

    sinks, shape = await collect(built, telemetry, [result.run_id])
    sinks["result"] = result.model_dump_json()
    # The model's own call, as it wrote it, stays in the run's messages (see the refused
    # tool arguments scenario); no other sink has the values the errors quote.
    assert_no_canary(
        sinks,
        scenario=f"schema errors ({on_error})",
        allowed={"store:run_store": set(PII_CANARIES)},
    )
    assert_golden(f"schema_errors.{on_error}", shape)
