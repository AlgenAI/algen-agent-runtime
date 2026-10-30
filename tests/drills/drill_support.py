"""Shared pieces of the persistence drills: databases, containers, a Runtime process and its crash.

A drill runs real Runtime containers (``build_container`` plus the app lifespan, as ``serve``
runs them) on live PostgreSQL and Redis. Each drill gets a database of its own on the server
``POSTGRES_DSN`` points at, so startup recovery only ever sees the drill's own runs.

The agent's one tool, ``drill.write``, writes a row to ``drill_side_effects`` in the same
database: an external effect every process can count. Its ``reconcile`` hook looks that row up,
as a real connector would ask the external system whether a call took effect.

``drill_child.py`` is a Runtime process the drills kill with SIGKILL: nothing it holds in memory
survives, exactly as in a crash.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.models.providers import mock as mock_module
from algen_agent_runtime.orchestration.container import Container, build_container
from algen_agent_runtime.tools.contracts import (
    ReconcileOutcome,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionRecord,
)
from algen_agent_runtime.types.contracts import (
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    TokenUsage,
    ToolCall,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SOURCE = ROOT / "src"
TENANT = "drill-tenant"
USER = "drill-user"
AGENT = "drill-agent"
APPROVAL_AGENT = "drill-approval-agent"
WORKFLOW = "drill-flow"
TOOL = "drill.write"
# Development header identity: the app runs behind ASGITransport, a loopback client.
HEADERS = {"x-tenant-id": TENANT, "x-user-id": USER, "x-scopes": "*"}

requires_postgres = pytest.mark.skipif(
    not os.environ.get("POSTGRES_DSN"), reason="the persistence drills need a live PostgreSQL"
)
requires_redis = pytest.mark.skipif(
    not os.environ.get("REDIS_URL"), reason="the Redis drills need a live Redis"
)


# --- Databases -------------------------------------------------------------------------------


def with_database(dsn: str, name: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path=f"/{name}"))


def database_name(dsn: str) -> str:
    return urlsplit(dsn).path.lstrip("/")


async def admin_execute(sql: str) -> None:
    import asyncpg

    connection = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        await connection.execute(sql)
    finally:
        await connection.close()


@asynccontextmanager
async def fresh_database(prefix: str = "drill") -> AsyncIterator[str]:
    """A new database on the drill server, empty but for ``drill_side_effects``; dropped
    afterwards."""
    name = f"{prefix}_{uuid4().hex[:12]}"
    await admin_execute(f'CREATE DATABASE "{name}"')
    try:
        dsn = with_database(os.environ["POSTGRES_DSN"], name)
        await ensure_side_effect_table(dsn)
        yield dsn
    finally:
        await admin_execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


async def query(dsn: str, sql: str, *arguments: Any) -> list[Any]:
    import asyncpg

    connection = await asyncpg.connect(dsn)
    try:
        return list(await connection.fetch(sql, *arguments))
    finally:
        await connection.close()


async def side_effects(dsn: str, run_id: str | None = None) -> list[str]:
    """The idempotency keys ``drill.write`` wrote, once per execution (duplicates repeat)."""
    sql = "SELECT idempotency_key FROM drill_side_effects"
    rows = (
        await query(dsn, sql + " WHERE run_id=$1 ORDER BY id", run_id)
        if run_id
        else await query(dsn, sql + " ORDER BY id")
    )
    return [str(row["idempotency_key"]) for row in rows]


async def ledger(dsn: str, run_id: str) -> list[tuple[str, str]]:
    """``(tool_name, status)`` of each recorded tool call of the run."""
    rows = await query(
        dsn,
        "SELECT tool_name, status FROM algen_agent_runtime_tool_executions "
        "WHERE run_id=$1 ORDER BY created_at",
        run_id,
    )
    return [(str(row["tool_name"]), str(row["status"])) for row in rows]


async def server_connections(dsn: str) -> int:
    """Connections to the database other than this one."""
    rows = await query(
        dsn,
        "SELECT count(*) AS n FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid()",
    )
    return int(rows[0]["n"])


MIGRATIONS = SOURCE / "algen_agent_runtime" / "persistence" / "migrations"


async def applied_migrations(dsn: str) -> list[str]:
    rows = await query(dsn, "SELECT name FROM algen_agent_runtime_schema_migrations ORDER BY name")
    return [str(row["name"]) for row in rows]


async def apply_migrations(dsn: str, through: str) -> list[str]:
    """Migrate an empty database as a release whose last migration is ``through`` (a prefix such
    as ``"014"``) left it, recording each in the migrations table as the Runtime does."""
    import asyncpg

    names = sorted(
        path.name for path in MIGRATIONS.glob("*.sql") if path.name.split("_")[0] <= through
    )
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS algen_agent_runtime_schema_migrations "
            "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        for name in names:
            async with connection.transaction():
                await connection.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
                await connection.execute(
                    "INSERT INTO algen_agent_runtime_schema_migrations (name) VALUES ($1)", name
                )
    finally:
        await connection.close()
    return names


# --- The previous release ----------------------------------------------------------------------

PREVIOUS_RELEASE = os.environ.get("ALGEN_DRILL_PREVIOUS_RELEASE", "v0.1.0a17")
_previous_source: dict[str, Path] = {}


def previous_release_source() -> Path:
    """The previous release's ``src`` directory, from its git tag; skips without the tag (CI
    checks out with ``fetch-depth: 0`` for the drills)."""
    if PREVIOUS_RELEASE in _previous_source:
        return _previous_source[PREVIOUS_RELEASE]
    repository = ROOT
    found = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--verify", "--quiet", PREVIOUS_RELEASE],
        capture_output=True,
        check=False,
    )
    if found.returncode != 0:
        pytest.skip(f"the previous release's tag {PREVIOUS_RELEASE} is not in this checkout")
    target = Path(tempfile.mkdtemp(prefix="algen-previous-release-"))
    archive = subprocess.run(
        ["git", "-C", str(repository), "archive", PREVIOUS_RELEASE, "src/algen_agent_runtime"],
        capture_output=True,
        check=True,
    )
    subprocess.run(["tar", "-x", "-C", str(target)], input=archive.stdout, check=True)
    _previous_source[PREVIOUS_RELEASE] = target / "src"
    return target / "src"


async def release_child(dsn: str, source: Path | None, **plan: Any) -> dict[str, Any]:
    """Run ``release_child.py`` on the code in ``source`` (the current code when None)."""
    code = source or SOURCE
    environment = {
        **os.environ,
        "DRILL_PLAN": json.dumps({"dsn": dsn, **plan}),
        "PYTHONPATH": str(code),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(HERE / "release_child.py"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 120)
    lines = [line for line in stdout.decode().splitlines() if line.startswith("RESULT ")]
    if process.returncode != 0 or not lines:
        raise AssertionError(
            f"release child failed ({process.returncode}): {stderr.decode()[-4000:]}"
        )
    result: dict[str, Any] = json.loads(lines[-1].removeprefix("RESULT "))
    # The process really ran the code it was given, not the installed package.
    assert str(result["module"]).startswith(str(code)), result["module"]
    return result


# --- Containers (the store servers) ------------------------------------------------------------


@dataclass(frozen=True)
class StoreContainer:
    """A store's container, which a drill pauses, unpauses or restarts.

    ``scripts/integration-tests.sh`` exports the engine and the container names.
    """

    engine: str
    name: str

    @classmethod
    def from_env(cls, variable: str) -> StoreContainer | None:
        engine = os.environ.get("ALGEN_TEST_CONTAINER_ENGINE")
        name = os.environ.get(variable)
        return cls(engine, name) if engine and name else None

    def run(self, *arguments: str, check: bool = True) -> str:
        completed = subprocess.run(
            [self.engine, *arguments], capture_output=True, text=True, check=False, timeout=120
        )
        if check and completed.returncode != 0:
            raise AssertionError(f"{self.engine} {' '.join(arguments)}: {completed.stderr}")
        return completed.stdout

    def pause(self) -> None:
        self.run("pause", self.name)

    def unpause(self) -> None:
        self.run("unpause", self.name)

    def restart(self) -> None:
        # Not stop/start: a stopped `--rm` container is removed with its data.
        self.run("restart", "--time", "2", self.name)

    def exec(self, *command: str, stdin: bytes | None = None) -> bytes:
        completed = subprocess.run(
            [self.engine, "exec", "-i", self.name, *command],
            input=stdin,
            capture_output=True,
            check=False,
            timeout=300,
        )
        if completed.returncode != 0:
            raise AssertionError(f"exec {command!r}: {completed.stderr.decode(errors='replace')}")
        return completed.stdout

    @contextmanager
    def paused(self) -> Iterator[None]:
        self.pause()
        try:
            yield
        finally:
            self.unpause()


def postgres_container() -> StoreContainer:
    found = StoreContainer.from_env("ALGEN_TEST_POSTGRES_CONTAINER")
    if found is None:
        pytest.skip("needs ALGEN_TEST_CONTAINER_ENGINE and ALGEN_TEST_POSTGRES_CONTAINER")
    return found


def redis_container() -> StoreContainer:
    found = StoreContainer.from_env("ALGEN_TEST_REDIS_CONTAINER")
    if found is None:
        pytest.skip("needs ALGEN_TEST_CONTAINER_ENGINE and ALGEN_TEST_REDIS_CONTAINER")
    return found


# --- The Runtime under drill -------------------------------------------------------------------


@dataclass
class Gate:
    """Where this process's runs stop and wait, until ``opened`` is set or the process is killed.

    ``after_tool``: the model call after a tool result, so the call is in the ledger and the
    effect is done but the run has not checkpointed the answer. ``in_tool``: inside the tool,
    after its effect, before it returns, so the ledger still says ``started``.
    """

    after_tool: bool = False
    in_tool: bool = False
    announce: Callable[[str], None] = field(default=lambda line: None)
    entered: int = 0
    # Set by an in-process drill to let held runs go on (and later ones pass straight through).
    opened: asyncio.Event = field(default_factory=asyncio.Event)

    async def hold(self, where: str, run_hint: str) -> None:
        if self.opened.is_set():
            return
        self.entered += 1
        self.announce(f"BLOCKED {where} {run_hint}")
        await self.opened.wait()


def provider_class(gate: Gate) -> type[mock_module.MockModelProvider]:
    """A deterministic model: call ``drill.write`` with the input, then answer once it has a
    result. It decides from the messages alone, so a recovered run gets the same replies."""

    class DrillProvider(mock_module.MockModelProvider):
        async def generate(self, request: ModelRequest) -> ModelResponse:
            last = request.messages[-1]
            if last.role is Role.TOOL:
                if gate.after_tool:
                    await gate.hold("after_tool", last.tool_call_id or "")
                return ModelResponse(
                    message=Message.text(Role.ASSISTANT, f"done: {last.text_content}"),
                    finish_reason=FinishReason.STOP,
                    usage=TokenUsage(input_tokens=10, output_tokens=3),
                    model=request.model or "deterministic",
                    provider=self.provider_id,
                )
            if not any(spec.name == TOOL for spec in request.tools):
                return await super().generate(request)
            user = next(m for m in reversed(request.messages) if m.role is Role.USER)
            return ModelResponse(
                message=Message.text(Role.ASSISTANT, ""),
                tool_calls=(ToolCall(name=TOOL, arguments={"value": user.text_content[:200]}),),
                finish_reason=FinishReason.TOOL_CALLS,
                usage=TokenUsage(input_tokens=10, output_tokens=3),
                model=request.model or "deterministic",
                provider=self.provider_id,
            )

    return DrillProvider


_SIDE_EFFECTS_DDL = (
    "CREATE TABLE IF NOT EXISTS drill_side_effects ("
    "id bigserial PRIMARY KEY, idempotency_key text NOT NULL, run_id text NOT NULL, "
    "value text NOT NULL, created_at timestamptz NOT NULL DEFAULT now())"
)


async def ensure_side_effect_table(dsn: str) -> None:
    import asyncpg

    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(_SIDE_EFFECTS_DDL)
    finally:
        await connection.close()


def side_effect_tool(dsn: str, gate: Gate) -> Tool:
    """``drill.write``: a keyed write whose effect is a row in ``drill_side_effects``."""

    async def write(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        import asyncpg

        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute(
                "INSERT INTO drill_side_effects (idempotency_key, run_id, value) "
                "VALUES ($1, $2, $3)",
                context.idempotency_key,
                context.run_id,
                str(arguments.get("value", "")),
            )
        finally:
            await connection.close()
        if gate.in_tool:
            await gate.hold("in_tool", context.run_id)
        return {"written": True}

    async def reconcile(record: ToolExecutionRecord, context: ToolContext) -> ReconcileOutcome:
        rows = await query(
            dsn,
            "SELECT 1 FROM drill_side_effects WHERE idempotency_key=$1",
            record.idempotency_key,
        )
        if rows:
            return ReconcileOutcome(status="completed", value={"written": True})
        return ReconcileOutcome(status="failed", detail="no row was written")

    return Tool(
        ToolDefinition(
            name=TOOL,
            version="1",
            description="Write a record.",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
            output_schema={"type": "object"},
            side_effect=SideEffect.WRITE,
            idempotency="keyed",
        ),
        write,
        reconcile=reconcile,
    )


def drill_settings(
    *,
    postgres: bool = True,
    redis: bool = False,
    runtime: dict[str, Any] | None = None,
    storage: dict[str, Any] | None = None,
    **extra: Any,
) -> AppSettings:
    agent = {
        "version": "1.0.0",
        "description": "Writes a record",
        "system_instructions": "Write the record, then report.",
        "default_model": {"name": "d", "provider": "mock", "model": "deterministic"},
        "enabled_tools": [TOOL],
        "max_steps": 6,
    }
    stores: dict[str, Any] = {}
    if postgres:
        stores.update(
            {
                name: "postgres"
                for name in (
                    "run_store",
                    "memory_store",
                    "event_store",
                    "audit_store",
                    "approval_store",
                    "artifact_store",
                    "workflow_store",
                    "tool_execution_store",
                    "conversation_store",
                )
            }
        )
        stores["postgres_dsn"] = "env://DRILL_POSTGRES_DSN"
    if redis:
        # Runs in Redis; the stores whose PostgreSQL rows reference a run stay in memory.
        stores.update(
            {
                "run_store": "redis",
                "event_store": "memory",
                "approval_store": "memory",
                "tool_execution_store": "memory",
                "artifact_store": "memory",
                "redis_url": "env://DRILL_REDIS_URL",
            }
        )
    stores.update(storage or {})
    values: dict[str, Any] = {
        "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
        "agents": [
            {
                **agent,
                "name": AGENT,
                "approval_policy": {"require_for_side_effects": False},
            },
            {**agent, "name": APPROVAL_AGENT},
        ],
        "workflows": {
            WORKFLOW: {
                "name": WORKFLOW,
                "version": "1.0.0",
                "output_key": "report",
                "nodes": [
                    {
                        "id": "work",
                        "kind": "agent",
                        "agent": AGENT,
                        "output_key": "report",
                        "input_template": "{{input}}",
                        "recovery_policy": "retry",
                    }
                ],
            }
        },
        "runtime": {"shutdown_grace_seconds": 0.5, **(runtime or {})},
        "storage": stores,
        **extra,
    }
    return AppSettings.model_validate(values)


@contextmanager
def _drill_provider(gate: Gate) -> Iterator[None]:
    original = mock_module.MockModelProvider
    mock_module.MockModelProvider = provider_class(gate)  # type: ignore[misc]
    try:
        yield
    finally:
        mock_module.MockModelProvider = original  # type: ignore[misc]


def build(
    dsn: str | None,
    *,
    gate: Gate | None = None,
    redis_url: str | None = None,
    settings: AppSettings | None = None,
) -> tuple[Container, Any]:
    """A Runtime process's container and app, not started."""
    gate = gate or Gate()
    settings = settings or drill_settings(postgres=dsn is not None, redis=redis_url is not None)
    environment = {
        **({"DRILL_POSTGRES_DSN": dsn} if dsn else {}),
        **({"DRILL_REDIS_URL": redis_url} if redis_url else {}),
    }
    tools = [side_effect_tool(dsn, gate)] if dsn else []
    with _drill_provider(gate):
        container = build_container(settings, environment=environment, additional_tools=tools)
    return container, create_app(settings, container)


@dataclass
class Process:
    """A started Runtime process: its container, app and an HTTP client on the app."""

    container: Container
    app: Any
    client: httpx.AsyncClient
    lifespan: Any

    @property
    def runtime(self) -> Any:
        return self.container.runtime

    async def stop(self) -> None:
        """A graceful shutdown: drain, then close every pool."""
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)


@asynccontextmanager
async def running(
    dsn: str | None,
    *,
    gate: Gate | None = None,
    redis_url: str | None = None,
    settings: AppSettings | None = None,
) -> AsyncIterator[Process]:
    """Start a Runtime process (recovering what the database holds) and stop it afterwards."""
    container, app = build(dsn, gate=gate, redis_url=redis_url, settings=settings)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://drill")
    process = Process(container, app, client, lifespan)
    stopped = False
    try:
        yield process
        await process.stop()
        stopped = True
    finally:
        if not stopped:
            await client.aclose()
            await lifespan.__aexit__(None, None, None)


async def until(condition: Callable[[], Any], what: str, within: float = 30.0) -> Any:
    """Poll ``condition`` (sync or async) until it returns something truthy."""
    deadline = asyncio.get_running_loop().time() + within
    while True:
        value = condition()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


# --- A Runtime process to kill -----------------------------------------------------------------


class Child:
    """``drill_child.py`` in a subprocess: it starts, recovers, does what it is told, reports
    what it did on stdout and then waits, until :meth:`kill` sends SIGKILL."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.lines: list[str] = []

    @classmethod
    async def spawn(cls, dsn: str, **plan: Any) -> Child:
        environment = {**os.environ, "DRILL_PLAN": json.dumps({"dsn": dsn, **plan})}
        source = str(SOURCE)
        environment["PYTHONPATH"] = os.pathsep.join(
            item for item in (source, environment.get("PYTHONPATH", "")) if item
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(HERE / "drill_child.py"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        return cls(process)

    async def expect(self, prefix: str, count: int = 1, within: float = 60.0) -> list[str]:
        """Read stdout until ``count`` lines start with ``prefix``; return their remainders."""
        assert self.process.stdout is not None

        async def read() -> None:
            while sum(line.startswith(prefix) for line in self.lines) < count:
                raw = await self.process.stdout.readline()  # type: ignore[union-attr]
                if not raw:
                    stderr = await self.process.stderr.read()  # type: ignore[union-attr]
                    raise AssertionError(
                        f"child exited before {count} x {prefix!r}: {self.lines}\n"
                        + stderr.decode(errors="replace")[-4000:]
                    )
                self.lines.append(raw.decode().strip())

        try:
            await asyncio.wait_for(read(), within)
        except TimeoutError:
            raise AssertionError(
                f"timed out waiting for {count} x {prefix!r}: {self.lines}"
            ) from None
        return [line[len(prefix) :].strip() for line in self.lines if line.startswith(prefix)]

    async def kill(self) -> None:
        if self.process.returncode is None:
            self.process.send_signal(signal.SIGKILL)
        await self.process.wait()
