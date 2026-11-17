"""Golden telemetry and redaction on live Redis: the canary scenarios with every store the
Runtime keeps in Redis (runs, session memory, the tool-result cache), and every key and value in
Redis read back afterwards (and while a run is paused).

Each test gets a Redis database of its own (``GOLDEN_REDIS_DB``, flushed before and after), so
every key in it came from the test. Run with ``scripts/integration-tests.sh tests/telemetry``;
the tests skip without ``REDIS_URL``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from golden_harness import (
    CALLER_CREDENTIAL,
    HELD_CANARIES,
    KEYED_RESULT_CANARIES,
    PII_CANARIES,
    SECRET_CANARIES,
    TENANT,
    USER,
    WORKING_DATA,
    Telemetry,
    assert_no_canary,
    capture_telemetry,
    leaky_mapping,
    leaky_text,
)
from golden_scenarios import (
    AGENT,
    agent_definition,
    collect,
    container,
    raising,
    request,
    returning,
    settings,
    tool,
    tool_call,
)

from algen_agent_runtime.conversations import MessageStatus
from algen_agent_runtime.orchestration.container import Container
from algen_agent_runtime.tools.ask_user import ASK_USER_PERMISSION, ASK_USER_TOOL
from algen_agent_runtime.types.contracts import RunStatus

pytestmark = [
    pytest.mark.integration,
    pytest.mark.redis,
    pytest.mark.skipif(
        not os.environ.get("REDIS_URL"), reason="golden Redis keys need a live Redis"
    ),
]

GOLDEN_REDIS_DB = int(os.environ.get("ALGEN_GOLDEN_REDIS_DB", "11"))


def _url() -> str:
    """REDIS_URL pointed at the suite's own database."""
    parts = urlsplit(os.environ["REDIS_URL"])
    return urlunsplit(parts._replace(path=f"/{GOLDEN_REDIS_DB}"))


@pytest.fixture
def telemetry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> Iterator[Telemetry]:
    with capture_telemetry(monkeypatch, caplog, capsys) as captured:
        yield captured


@pytest.fixture
async def redis() -> AsyncIterator[Any]:
    asyncio_redis = pytest.importorskip("redis.asyncio")
    client = asyncio_redis.Redis.from_url(_url())
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


def _redis_settings(*agents: dict[str, Any], **extra: Any) -> Any:
    return settings(
        *agents,
        storage={
            "run_store": "redis",
            "memory_store": "redis",
            "redis_url": "env://GOLDEN_REDIS_URL",
        },
        cache={
            "backend": "redis",
            "redis_url": "env://GOLDEN_REDIS_URL",
            "policies": {"tool_results": {"enabled": True}},
        },
        **extra,
    )


def _container(app: Any, *args: Any, **kwargs: Any) -> Container:
    return container(app, *args, environment={"GOLDEN_REDIS_URL": _url()}, **kwargs)


async def _keys(redis: Any) -> dict[str, str]:
    """Every key in the database and its whole value, whatever its type, one string per key
    family (``redis:run``, ``redis:memory``, ``redis:cache``, ``redis:other``)."""
    families: dict[str, list[str]] = {}
    async for raw in redis.scan_iter(match="*", count=1000):
        key = raw.decode() if isinstance(raw, bytes) else str(raw)
        kind = await redis.type(key)
        kind = kind.decode() if isinstance(kind, bytes) else str(kind)
        if kind == "string":
            values: Any = [await redis.get(key)]
        elif kind == "hash":
            values = [item for pair in (await redis.hgetall(key)).items() for item in pair]
        elif kind == "list":
            values = await redis.lrange(key, 0, -1)
        elif kind == "set":
            values = list(await redis.smembers(key))
        elif kind == "zset":
            values = [member for member, _ in await redis.zrange(key, 0, -1, withscores=True)]
        else:
            values = [f"<{kind}>"]
        text = "\n".join(
            value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
            for value in values
        )
        if ":cache:" in key:
            family = "redis:cache"
        elif key.endswith(":memory") or ":usage:memory" in key or key.endswith(":owner"):
            family = "redis:memory"
        elif ":run:" in key or ":turn:" in key:
            family = "redis:run"
        else:
            family = "redis:other"
        families.setdefault(family, []).append(f"{key}\n{text}")
    return {family: "\n".join(entries) for family, entries in families.items()}


async def test_model_tool_memory_and_cache_keys(telemetry: Telemetry, redis: Any) -> None:
    """The model and tool path on Redis stores, twice in one session: session memory, the run
    state and the tool-result cache (the second call is a cache hit). No canary in any key or
    value but the run's working data."""
    calls: list[str] = []

    async def lookup(arguments: dict[str, Any], context: Any) -> Any:
        calls.append(arguments["query"])
        return leaky_mapping("tool result")

    built = _container(
        _redis_settings(agent_definition(enabled_tools=["crm.lookup"])),
        [tool("crm.lookup", lookup)],
        [
            tool_call("crm.lookup", {"query": "customer 7"}),
            leaky_text("answer"),
            tool_call("crm.lookup", {"query": "customer 7"}),
            leaky_text("second answer"),
        ],
    )
    try:
        first = await built.runtime.run(request(leaky_text("input"), session_id="golden-session"))
        second = await built.runtime.run(
            request(leaky_text("second input"), session_id="golden-session")
        )
        assert first.status == second.status == RunStatus.COMPLETED, (first.error, second.error)
        assert len(calls) == 1, "the second call is served from the Redis cache"
        sinks, _ = await collect(built, telemetry, [first.run_id, second.run_id])
        assert any(
            name == "algen_agent_runtime.cache.operations"
            and attributes.get("cache.backend") == "redis"
            and attributes.get("cache.outcome") == "hit"
            for name, attributes in telemetry.metric_points()
        )
    finally:
        await built.aclose()
    keys = await _keys(redis)
    assert {"redis:run", "redis:memory", "redis:cache"} <= set(keys), sorted(keys)
    sinks.update(keys)
    assert_no_canary(
        sinks,
        scenario="redis model, tool, memory and cache",
        allowed={
            **WORKING_DATA,
            # The run's state and the cached result keep the tool's keyed field, as the
            # tool-execution ledger does (golden_harness.WORKING_DATA).
            "redis:run": KEYED_RESULT_CANARIES,
            "redis:cache": KEYED_RESULT_CANARIES,
        },
    )
    assert_no_canary(sinks, HELD_CANARIES, scenario="redis model, tool, memory and cache (held)")


async def test_paused_run_with_steering_and_a_failure(telemetry: Telemetry, redis: Any) -> None:
    """A run paused on ``core.ask_user`` with steering waiting (read while paused, when the
    run's state in Redis holds it), answered and finished; and a run a tool fails."""
    built = _container(
        _redis_settings(
            agent_definition(
                enabled_tools=[ASK_USER_TOOL, "billing.charge"],
                tool_permissions=[ASK_USER_PERMISSION],
            )
        ),
        [tool("billing.charge", raising(RuntimeError(leaky_text("tool error"))))],
        [
            tool_call(ASK_USER_TOOL, {"question": "Which account?"}),
            leaky_text("answer"),
            tool_call("billing.charge", {"query": "invoice 9"}),
            "sorry",
        ],
    )
    try:
        paused = await built.runtime.run(request(leaky_text("input"), session_id="golden-pause"))
        assert paused.status == RunStatus.AWAITING_CLARIFICATION, paused.error
        await built.runtime.steer(paused.run_id, TENANT, leaky_text("steer"))
        while_paused = await _keys(redis)
        await built.runtime.resume(
            paused.run_id, TENANT, {"clarification": leaky_text("answer to ask_user")}
        )
        answered = await built.runtime.wait(paused.run_id, TENANT)
        assert answered.status == RunStatus.COMPLETED, answered.error
        failed = await built.runtime.run(request("charge it", session_id="golden-fail"))
        assert failed.status == RunStatus.FAILED
        sinks, _ = await collect(built, telemetry, [paused.run_id, failed.run_id])
    finally:
        await built.aclose()
    sinks.update({f"{name} (paused)": text for name, text in while_paused.items()})
    sinks.update(await _keys(redis))
    sinks["results"] = answered.model_dump_json() + failed.model_dump_json()
    assert_no_canary(sinks, scenario="redis paused run and failure", allowed=WORKING_DATA)


async def _settled(service: Any, conversation_id: str, *wanted: MessageStatus) -> Any:
    statuses = set(wanted) or {MessageStatus.COMPLETED, MessageStatus.FAILED}
    for _ in range(1200):
        messages = await service.messages(conversation_id, TENANT)
        if messages and messages[-1].status in statuses:
            return messages[-1]
        await asyncio.sleep(0.01)
    raise AssertionError(f"the turn never reached {statuses}")


async def test_conversation_turn_with_a_caller_credential(telemetry: Telemetry, redis: Any) -> None:
    """A conversation turn on Redis run and memory stores, with the caller's credential: it
    reaches no key, and the turn's run keeps only its working data."""
    built = _container(
        _redis_settings(agent_definition(enabled_tools=["crm.lookup"])),
        [tool("crm.lookup", returning(leaky_mapping("tool result")))],
        [tool_call("crm.lookup", {"query": "customer 7"}), leaky_text("answer")],
    )
    try:
        service = built.conversations
        conversation = await service.create(tenant_id=TENANT, user_id=USER, agent=AGENT)
        await service.submit(
            conversation.id,
            TENANT,
            USER,
            leaky_text("user message"),
            credentials={"authorization": f"Bearer {CALLER_CREDENTIAL}"},
        )
        done = await _settled(service, conversation.id)
        assert done.status == MessageStatus.COMPLETED, done
        sinks, _ = await collect(built, telemetry, list(done.run_ids))
    finally:
        await built.aclose()
    sinks.update(await _keys(redis))
    users_own = set(SECRET_CANARIES) | set(PII_CANARIES)
    assert_no_canary(
        sinks,
        scenario="redis conversation turn",
        allowed={
            **WORKING_DATA,
            "redis:run": KEYED_RESULT_CANARIES,
            "redis:cache": KEYED_RESULT_CANARIES,
            "store:conversation_store": users_own,
            "store:conversation_events": users_own,
        },
    )
    assert_no_canary(sinks, HELD_CANARIES, scenario="redis conversation turn (held)")
