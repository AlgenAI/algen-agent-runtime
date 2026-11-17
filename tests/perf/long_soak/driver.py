"""The long soak's load driver: HTTP load, chaos, sampling and the end-of-soak invariants.

Run by ``scripts/long-soak.sh``, which starts the containers and passes the arguments; see
``README.md``. It talks to the Runtime only over HTTP (JWT-authenticated, as a host would) and to
PostgreSQL, Redis and the container engine only to observe and to restart them.

Output, in ``--out``:

- ``windows.jsonl``: one line per ``--window-seconds``: operations, p50/p95/p99 latency per
  operation, errors by class, and whether the noisy tenant was flooding.
- ``samples.jsonl``: one line per ``--sample-seconds``: the Runtime's RSS, open FDs, threads, CPU,
  asyncio tasks (from the plugin's probe), PostgreSQL connections, table sizes and dead tuples,
  Redis memory.
- ``chaos.jsonl``: each restart, how long until ready again.
- ``journal.jsonl``: every run, conversation and workflow the driver started and what it expects.
- ``summary.json``: totals, overall latency, the invariants and whether each held.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import jwt
import redis.asyncio as aioredis

SCOPES = (
    "runs:read runs:write approvals:read approvals:decide conversations:read "
    "conversations:write artifacts:read artifacts:write usage:read"
)
TERMINAL = {"completed", "failed", "cancelled", "timed_out"}
# Rows the soak aged that the maintenance sweep has not deleted yet.
AGED_ROWS = (
    "SELECT (SELECT count(*) FROM algen_agent_runtime_runs WHERE updated_at < "
    "now() - interval '1 day') + (SELECT count(*) FROM "
    "algen_agent_runtime_audit_events WHERE created_at < now() - interval '1 day') + "
    "(SELECT count(*) FROM algen_agent_runtime_conversations WHERE updated_at < "
    "now() - interval '1 day') + (SELECT count(*) FROM "
    "algen_agent_runtime_workflow_checkpoints WHERE updated_at < now() - "
    "interval '1 day')"
)
RUN_END = {"run.completed", "run.failed", "run.cancelled"}
RUN_PAUSE = {
    "approval.required",
    "clarification.required",
    "tool.reconciliation_required",
    "credentials.required",
}
TURN_END = {
    "assistant.message.completed",
    "assistant.message.failed",
    "assistant.message.cancelled",
    "assistant.message.expired",
}
TURN_PAUSE = {"assistant.message.awaiting_approval", "assistant.message.awaiting_input"}


def latest_probe(directory: Path) -> dict[str, Any] | None:
    """The last line the plugin's probe wrote (the newest process's file)."""
    files = sorted(directory.glob("runtime-*.jsonl"), key=os.path.getmtime)
    lines = files[-1].read_text().splitlines() if files else []
    return json.loads(lines[-1]) if lines else None


def read_effects(directory: Path) -> tuple[Counter[str], dict[str, str]]:
    """``soak.record`` effects: how many times each idempotency key happened, and its run."""
    effects: Counter[str] = Counter()
    tenants: dict[str, str] = {}
    for path in directory.glob("effects-*.jsonl"):
        for line in path.read_text().splitlines():
            if line.strip():
                item = json.loads(line)
                effects[item["key"]] += 1
                tenants[item["key"]] = item["run_id"]
    return effects, tenants


class OpError(Exception):
    """An operation failed; ``kind`` is its error class."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-fraction * len(ordered) // 1))))
    return ordered[rank - 1]


def latency_summary(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p50_ms": round(percentile(values, 0.5) * 1000, 1),
        "p95_ms": round(percentile(values, 0.95) * 1000, 1),
        "p99_ms": round(percentile(values, 0.99) * 1000, 1),
        "max_ms": round(max(values) * 1000, 1) if values else None,
    }


@dataclass
class Window:
    started: float
    ok: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    latency: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    noisy_accepted: int = 0
    noisy_refused: int = 0


class Soak:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.out = Path(args.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.key = Path(args.private_key).read_text()
        self.began = time.time()
        self.deadline = self.began + args.duration
        self.stopping = False
        self.noisy = False
        self.chaos_active = False
        self.ready = asyncio.Event()
        self.ready.set()
        self.window = Window(time.time())
        self.totals_ok: Counter[str] = Counter()
        self.totals_errors: Counter[str] = Counter()
        self.all_latency: dict[str, list[float]] = defaultdict(list)
        self.quiet_latency: dict[str, list[float]] = defaultdict(list)
        self.noisy_latency: dict[str, list[float]] = defaultdict(list)
        self.outcomes: Counter[str] = Counter()
        self.noisy_totals = Counter[str]()
        self.journal = (self.out / "journal.jsonl").open("a", encoding="utf-8")
        self.tokens: dict[tuple[str, str], tuple[str, float]] = {}
        self.client = httpx.AsyncClient(
            base_url=args.base_url,
            timeout=httpx.Timeout(60, connect=10),
            limits=httpx.Limits(max_connections=1000, max_keepalive_connections=200),
        )
        self.decided: dict[str, str] = {}
        self.restarts: list[dict[str, Any]] = []
        self.retire_cycles: list[dict[str, Any]] = []
        self.cpu_ticks: tuple[Any, int, float] | None = None
        self.retired: set[str] = set()
        self.last_retired: list[str] = []
        self.rolling: Counter[str] = Counter()
        self.effects: Counter[str] = Counter()
        self.effect_runs: dict[str, str] = {}
        self.effect_offsets: dict[str, int] = {}

    # --- plumbing -------------------------------------------------------------------------------

    def token(self, tenant: str, user: str) -> str:
        cached = self.tokens.get((tenant, user))
        now = time.time()
        if cached and cached[1] > now + 600:
            return cached[0]
        claims = {
            "sub": user,
            "tenant_id": tenant,
            "scope": SCOPES,
            "iss": "https://soak.invalid/",
            "aud": "algen-agent-runtime",
            "iat": int(now),
            "exp": int(now + 3600),
            "jti": uuid.uuid4().hex,
        }
        value = jwt.encode(claims, self.key, algorithm="RS256")
        self.tokens[(tenant, user)] = (value, now + 3600)
        return value

    def headers(self, tenant: str, user: str) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token(tenant, user)}"}

    def record(self, kind: str, **values: Any) -> None:
        self.journal.write(json.dumps({"kind": kind, "at": round(time.time(), 3), **values}) + "\n")

    def ok(self, op: str, seconds: float | None = None) -> None:
        self.window.ok[op] += 1
        self.totals_ok[op] += 1
        if seconds is not None:
            self.window.latency[op].append(seconds)
            self.all_latency[op].append(seconds)
            (self.noisy_latency if self.noisy else self.quiet_latency)[op].append(seconds)

    def error(self, op: str, kind: str, detail: str = "") -> None:
        label = f"{op}:{kind}" + (":chaos" if self.chaos_active else "")
        self.window.errors[label] += 1
        self.totals_errors[label] += 1
        # The first few of each kind, with what the Runtime said, in errors.jsonl.
        if detail and self.totals_errors[label] <= 20:
            self.append(
                "errors.jsonl", {"at": round(time.time(), 1), "label": label, "detail": detail}
            )

    async def request(
        self,
        method: str,
        url: str,
        tenant: str,
        user: str,
        *,
        op: str,
        expect: tuple[int, ...] = (200, 201, 202, 204),
        retry_unavailable: float = 180,
        **kwargs: Any,
    ) -> httpx.Response:
        """One HTTP call. While the Runtime restarts (connection refused, 503) it is retried, as a
        host would, for up to ``retry_unavailable`` seconds; each failed attempt is counted."""
        give_up = time.monotonic() + retry_unavailable
        while True:
            try:
                response = await self.client.request(
                    method, url, headers=self.headers(tenant, user), **kwargs
                )
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                self.error(op, "unavailable")
                if time.monotonic() > give_up:
                    raise OpError("unavailable", type(exc).__name__) from exc
                await asyncio.sleep(1)
                await self.wait_ready(give_up)
                continue
            except httpx.TimeoutException as exc:
                self.error(op, "timeout")
                raise OpError("timeout", type(exc).__name__) from exc
            if response.status_code in expect:
                return response
            if response.status_code == 503 and time.monotonic() < give_up:
                self.error(op, "http_503")
                await asyncio.sleep(1)
                continue
            if response.status_code == 429:
                raise OpError("rate_limited", response.headers.get("retry-after", ""))
            kind = f"http_{response.status_code}"
            raise OpError(kind, f"{method} {url}: {response.text[:300]}")

    async def wait_ready(self, give_up: float) -> None:
        while time.monotonic() < give_up:
            with contextlib.suppress(httpx.HTTPError):
                response = await self.client.get("/health/ready", timeout=5)
                if response.status_code == 200:
                    return
            await asyncio.sleep(1)

    async def sse(
        self, url: str, tenant: str, user: str, last_id: int, op: str
    ) -> AsyncIterator[tuple[int, str, dict[str, Any]]]:
        """Events from an SSE route after ``last_id``; reconnects (with Last-Event-ID) when the
        stream breaks, as a host would, and stops when the caller stops iterating."""
        give_up = time.monotonic() + 900
        while True:
            headers = self.headers(tenant, user)
            if last_id:
                headers["last-event-id"] = str(last_id)
            try:
                async with self.client.stream(
                    "GET", url, headers=headers, timeout=httpx.Timeout(120, connect=10)
                ) as response:
                    if response.status_code == 429:
                        self.error(op, "stream_429")
                        await asyncio.sleep(2)
                        continue
                    if response.status_code == 503:
                        self.error(op, "stream_503")
                        await asyncio.sleep(1)
                        continue
                    if response.status_code != 200:
                        raise OpError(f"stream_http_{response.status_code}")
                    event_id, event_type, data = 0, "", ""
                    async for line in response.aiter_lines():
                        if line.startswith("id: "):
                            event_id = int(line[4:])
                        elif line.startswith("event: "):
                            event_type = line[7:]
                        elif line.startswith("data: "):
                            data = line[6:]
                        elif line == "" and event_type:
                            if event_id > last_id:
                                last_id = event_id
                            if event_type == "stream.overflow":
                                self.error(op, "stream_overflow")
                                break
                            yield event_id, event_type, json.loads(data) if data else {}
                            event_type, data = "", ""
                    else:
                        # The server ended the stream (end or pause event already yielded, or
                        # a drain): the caller decides whether to follow again.
                        return
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                self.error(op, "stream_broken", f"GET {url}: {type(exc).__name__}: {exc}")
            except httpx.TimeoutException:
                self.error(op, "stream_timeout")
            if time.monotonic() > give_up:
                raise OpError("follow_timeout")
            await asyncio.sleep(1)
            await self.wait_ready(give_up)

    async def follow_run(
        self, run_id: str, tenant: str, user: str, op: str, after: int = 0
    ) -> tuple[str, dict[str, Any], int]:
        """Follow a run's events to its next pause or end: (event type, data, last sequence).
        A stream that ends with neither (a drain) is opened again."""
        last = after
        for _ in range(30):
            async for sequence, kind, data in self.sse(
                f"/v1/runs/{run_id}/events", tenant, user, last, op
            ):
                last = sequence
                if kind in RUN_END or kind in RUN_PAUSE:
                    return kind, data.get("data", {}), last
            await asyncio.sleep(0.5)
            # The stream ended without an end or pause: look at the run before following again.
            state = await self.run_status(run_id, tenant, user, op)
            if state in TERMINAL:
                return f"status.{state}", {}, last
        raise OpError("follow_exhausted")

    async def run_status(self, run_id: str, tenant: str, user: str, op: str) -> str:
        response = await self.request("GET", f"/v1/runs/{run_id}", tenant, user, op=op)
        return str(response.json()["status"])

    async def create_run(
        self, tenant: str, user: str, agent: str, text: str, op: str, *, journal: bool = True
    ) -> str:
        response = await self.request(
            "POST",
            "/v1/runs",
            tenant,
            user,
            op=op,
            json={"agent": agent, "input": text},
            retry_unavailable=180,
        )
        run_id = str(response.json()["run_id"])
        if journal:
            self.record("run", run_id=run_id, tenant=tenant, user=user, agent=agent, op=op)
        return run_id

    # --- workloads ------------------------------------------------------------------------------

    async def reader_run(self, tenant: str, user: str) -> None:
        op = "run.reader"
        started = time.monotonic()
        run_id = await self.create_run(tenant, user, "soak-reader", "do: lookup,lookup now", op)
        kind, _, _ = await self.follow_run(run_id, tenant, user, op)
        outcome = kind.removeprefix("run.").removeprefix("status.")
        self.outcomes[f"{op}:{outcome}"] += 1
        if outcome != "completed":
            raise OpError(f"run_{outcome}")
        self.ok(op, time.monotonic() - started)

    async def writer_run(self, tenant: str, user: str) -> None:
        op = "run.approval"
        started = time.monotonic()
        run_id = await self.create_run(tenant, user, "soak-writer", "do: lookup,record now", op)
        kind, _, last = await self.follow_run(run_id, tenant, user, op)
        if kind != "approval.required":
            self.outcomes[f"{op}:unexpected:{kind}"] += 1
            raise OpError("no_approval", kind)
        self.ok("run.approval.pause", time.monotonic() - started)
        roll = random.random()
        if roll < self.args.leave_approval_fraction:
            self.decided[run_id] = "left"
            self.record("decision", run_id=run_id, decision="left", tenant=tenant)
            self.outcomes[f"{op}:left_to_expire"] += 1
            self.ok(op)
            return
        decision = "rejected" if roll < self.args.leave_approval_fraction + 0.1 else "approved"
        await asyncio.sleep(random.uniform(0.2, 3))
        decided = time.monotonic()
        try:
            await self.request(
                "POST",
                f"/v1/runs/{run_id}/approval",
                tenant,
                user,
                op=op,
                json={"decision": decision, "comment": "soak"},
            )
        except OpError as exc:
            self.record("decision_refused", run_id=run_id, decision=decision, error=exc.kind)
            raise
        self.decided[run_id] = decision
        self.record("decision", run_id=run_id, decision=decision, tenant=tenant)
        kind, _, _ = await self.follow_run(run_id, tenant, user, op, after=last)
        outcome = kind.removeprefix("run.").removeprefix("status.")
        self.outcomes[f"{op}:{decision}:{outcome}"] += 1
        expected = "completed" if decision == "approved" else "failed"
        if outcome != expected:
            raise OpError(f"{decision}_ended_{outcome}")
        self.ok("run.approval.decided", time.monotonic() - decided)
        self.ok(op, time.monotonic() - started)

    async def cancelled_run(self, tenant: str, user: str) -> None:
        op = "run.cancel"
        run_id = await self.create_run(tenant, user, "soak-slow", "do: lookup,lookup,lookup", op)
        await asyncio.sleep(random.uniform(0.5, 5))
        started = time.monotonic()
        await self.request("DELETE", f"/v1/runs/{run_id}", tenant, user, op=op, expect=(200, 409))
        for _ in range(240):
            state = await self.run_status(run_id, tenant, user, op)
            if state in TERMINAL:
                self.outcomes[f"{op}:{state}"] += 1
                if state not in {"cancelled", "completed"}:
                    raise OpError(f"cancel_ended_{state}")
                self.ok(op, time.monotonic() - started)
                return
            await asyncio.sleep(0.25)
        raise OpError("cancel_not_terminal")

    async def slow_run(self, tenant: str, user: str) -> None:
        op = "run.slow"
        started = time.monotonic()
        run_id = await self.create_run(tenant, user, "soak-slow", "do: lookup,lookup", op)
        kind, _, _ = await self.follow_run(run_id, tenant, user, op)
        outcome = kind.removeprefix("run.").removeprefix("status.")
        self.outcomes[f"{op}:{outcome}"] += 1
        if outcome != "completed":
            raise OpError(f"run_{outcome}")
        self.ok(op, time.monotonic() - started)

    async def artifact(self, tenant: str, user: str) -> None:
        op = "artifact"
        started = time.monotonic()
        payload = os.urandom(random.randint(8_192, 262_144))
        response = await self.request(
            "POST",
            "/v1/artifacts",
            tenant,
            user,
            op=op,
            params={
                "name": f"soak-{uuid.uuid4().hex[:8]}.bin",
                "media_type": "application/octet-stream",
                "expires_in_seconds": random.choice((60, 120, 300)),
            },
            content=payload,
        )
        artifact_id = response.json()["id"]
        download = await self.request("GET", f"/v1/artifacts/{artifact_id}", tenant, user, op=op)
        if hashlib.sha256(download.content).hexdigest() != hashlib.sha256(payload).hexdigest():
            raise OpError("artifact_corrupt")
        self.ok(op, time.monotonic() - started)

    async def workflow(self, tenant: str, user: str) -> None:
        op = "workflow"
        started = time.monotonic()
        workflow_id = f"wf-{uuid.uuid4().hex}"
        await self.request(
            "POST",
            "/v1/workflows/soak-review/runs",
            tenant,
            user,
            op=op,
            json={
                "inputs": {"text": "do: lookup draft", "key": uuid.uuid4().hex},
                "workflow_id": workflow_id,
            },
        )
        self.record("workflow", workflow_id=workflow_id, tenant=tenant, user=user)
        state = await self.poll_workflow(workflow_id, tenant, user, {"awaiting_approval"}, op)
        if state != "awaiting_approval":
            self.outcomes[f"{op}:unexpected:{state}"] += 1
            raise OpError(f"workflow_{state}")
        self.ok("workflow.pause", time.monotonic() - started)
        roll = random.random()
        if roll < self.args.leave_approval_fraction:
            self.record("workflow_decision", workflow_id=workflow_id, decision="left")
            self.outcomes[f"{op}:left_to_expire"] += 1
            self.ok(op)
            return
        decision = "rejected" if roll < self.args.leave_approval_fraction + 0.1 else "approved"
        await asyncio.sleep(random.uniform(0.2, 3))
        await self.request(
            "POST",
            f"/v1/workflows/runs/{workflow_id}/approval",
            tenant,
            user,
            op=op,
            json={"decision": decision},
        )
        self.record("workflow_decision", workflow_id=workflow_id, decision=decision)
        state = await self.poll_workflow(workflow_id, tenant, user, set(), op)
        self.outcomes[f"{op}:{decision}:{state}"] += 1
        if state != "completed":
            raise OpError(f"workflow_{decision}_{state}")
        self.ok(op, time.monotonic() - started)

    async def poll_workflow(
        self, workflow_id: str, tenant: str, user: str, stop: set[str], op: str
    ) -> str:
        for _ in range(600):
            response = await self.request(
                "GET", f"/v1/workflows/runs/{workflow_id}", tenant, user, op=op
            )
            state = str(response.json()["status"])
            if state in stop or state in {"completed", "failed", "cancelled"}:
                return state
            await asyncio.sleep(0.5)
        raise OpError("workflow_poll_timeout")

    async def conversation_worker(self, tenant: str, user: str) -> None:
        """One user chatting: turns in one conversation, a new conversation every few turns
        (the old one deleted half the time)."""
        op = "turn"
        conversation: str | None = None
        last_seq = 0
        turns = 0
        while not self.stopping and time.time() < self.deadline:
            try:
                if conversation is None or turns >= random.randint(5, 20):
                    if conversation is not None and random.random() < 0.5:
                        await self.request(
                            "DELETE", f"/v1/conversations/{conversation}", tenant, user, op=op
                        )
                        self.record("conversation_deleted", conversation_id=conversation)
                    response = await self.request(
                        "POST",
                        "/v1/conversations",
                        tenant,
                        user,
                        op=op,
                        json={"agent": "soak-chat", "title": "soak"},
                    )
                    conversation = str(response.json()["id"])
                    self.record("conversation", conversation_id=conversation, tenant=tenant)
                    last_seq, turns = 0, 0
                last_seq = await self.turn(conversation, tenant, user, last_seq)
                turns += 1
            except OpError as exc:
                self.error(op, exc.kind, exc.detail)
                if exc.kind in {"http_404", "http_409"}:
                    conversation = None
            except Exception as exc:  # the soak goes on; the error is counted
                self.error(op, f"exception_{type(exc).__name__}")
                conversation = None
            await asyncio.sleep(random.uniform(*self.args.think))

    async def turn(self, conversation: str, tenant: str, user: str, last_seq: int) -> int:
        op = "turn"
        write = random.random() < 0.2
        text = "do: lookup,record please" if write else "do: lookup please"
        started = time.monotonic()
        response = await self.request(
            "POST",
            f"/v1/conversations/{conversation}/messages",
            tenant,
            user,
            op=op,
            json={"text": text},
        )
        body = response.json()
        turn_id, assistant = str(body["message_id"]), str(body["assistant_message_id"])
        cancel = random.random() < 0.05
        if cancel:
            await asyncio.sleep(random.uniform(0.05, 0.4))
            await self.request(
                "DELETE", f"/v1/conversations/{conversation}/active-response", tenant, user, op=op
            )
        decided = False
        decision = ""
        first_delta: float | None = None
        async for sequence, kind, data in self.sse(
            f"/v1/conversations/{conversation}/events", tenant, user, last_seq, op
        ):
            last_seq = sequence
            if data.get("message_id") != assistant:
                continue
            if kind == "assistant.message.delta" and first_delta is None:
                first_delta = time.monotonic() - started
                self.ok("turn.first_delta", first_delta)
            if kind in TURN_PAUSE:
                if decided:
                    continue
                if cancel:
                    # The turn paused around the cancel: deciding it would be refused (409) if
                    # the cancel landed; cancelling again ends it either way.
                    await self.request(
                        "DELETE",
                        f"/v1/conversations/{conversation}/active-response",
                        tenant,
                        user,
                        op=op,
                    )
                    decided = True
                    continue
                decision = "rejected" if random.random() < 0.1 else "approved"
                await self.request(
                    "POST",
                    f"/v1/conversations/{conversation}/turns/{turn_id}/approval",
                    tenant,
                    user,
                    op=op,
                    json={"decision": decision},
                )
                self.record(
                    "turn_decision",
                    conversation_id=conversation,
                    turn_id=turn_id,
                    decision=decision,
                )
                decided = True
                continue
            if kind in TURN_END:
                outcome = kind.rsplit(".", 1)[-1]
                label = f"{op}:{'write' if write else 'read'}:{decision or 'none'}:{outcome}"
                self.outcomes[label] += 1
                # A rejected write ends the turn failed ("Proposed action was rejected."): the
                # agent's on_rejection is fail_run.
                expected = {"completed"} if decision != "rejected" else {"failed"}
                if cancel:
                    expected.add("cancelled")
                if outcome in expected:
                    self.ok(op + (".approval" if write else ""), time.monotonic() - started)
                    return last_seq
                raise OpError(f"turn_{outcome}")
        raise OpError("turn_stream_ended")

    async def worker(self, tenant: str, user: str, index: int) -> None:
        kinds: list[tuple[str, Callable[[str, str], Coroutine[Any, Any, None]], float]] = [
            ("run.reader", self.reader_run, 0.35),
            ("run.approval", self.writer_run, 0.2),
            ("workflow", self.workflow, 0.1),
            ("run.cancel", self.cancelled_run, 0.08),
            ("run.slow", self.slow_run, 0.07),
            ("artifact", self.artifact, 0.2),
        ]
        weights = [weight for _, _, weight in kinds]
        await asyncio.sleep(random.uniform(0, 5))
        while not self.stopping and time.time() < self.deadline:
            name, work, _ = random.choices(kinds, weights)[0]
            try:
                await work(tenant, user)
            except OpError as exc:
                self.error(name, exc.kind, exc.detail)
                if exc.kind == "rate_limited":
                    await asyncio.sleep(1)
            except Exception as exc:  # the soak goes on; the error is counted
                self.error(name, f"exception_{type(exc).__name__}")
            await asyncio.sleep(random.uniform(*self.args.think))

    async def noisy_client(self) -> None:
        """The noisy neighbour: while flooding, create slow runs back to back and retry a
        refusal after 100 ms whatever Retry-After says."""
        tenant, user = "t-noisy", f"noisy-{random.randint(1, 5)}"
        while not self.stopping and time.time() < self.deadline:
            if not self.noisy:
                await asyncio.sleep(1)
                continue
            try:
                response = await self.client.post(
                    "/v1/runs",
                    headers=self.headers(tenant, user),
                    json={"agent": "soak-slow", "input": "do: lookup"},
                    timeout=30,
                )
                if response.status_code == 202:
                    self.window.noisy_accepted += 1
                    self.noisy_totals["accepted"] += 1
                    self.record(
                        "run",
                        run_id=response.json()["run_id"],
                        tenant=tenant,
                        user=user,
                        agent="soak-slow",
                        op="noisy",
                    )
                elif response.status_code == 429:
                    self.window.noisy_refused += 1
                    self.noisy_totals["refused"] += 1
                else:
                    self.noisy_totals[f"http_{response.status_code}"] += 1
            except httpx.HTTPError as exc:
                self.noisy_totals[type(exc).__name__] += 1
                await asyncio.sleep(1)
            await asyncio.sleep(0.1)

    async def noisy_schedule(self) -> None:
        period, length = self.args.noisy_period, self.args.noisy_length
        await asyncio.sleep(self.args.noisy_first)
        while not self.stopping and time.time() < self.deadline - length:
            self.noisy = True
            await asyncio.sleep(length)
            self.noisy = False
            await asyncio.sleep(max(0, period - length))

    # --- chaos ----------------------------------------------------------------------------------

    def engine(self, *args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.args.engine, *args], capture_output=True, text=True, timeout=timeout, check=False
        )

    async def restart_runtime(self, signal: str) -> None:
        name = self.args.runtime_container
        self.chaos_active = True
        began = time.monotonic()
        entry: dict[str, Any] = {"at": round(time.time(), 1), "action": f"runtime_{signal}"}
        if signal == "TERM":
            result = await asyncio.to_thread(
                self.engine, "stop", "-t", str(self.args.stop_timeout), name
            )
            entry["stop_seconds"] = round(time.monotonic() - began, 2)
            inspect = await asyncio.to_thread(
                self.engine, "inspect", "-f", "{{.State.ExitCode}}", name
            )
            entry["exit_code"] = inspect.stdout.strip()
        else:
            result = await asyncio.to_thread(self.engine, "kill", "-s", "KILL", name)
            await asyncio.to_thread(self.engine, "wait", name)
        entry["stop_rc"] = result.returncode
        await asyncio.to_thread(self.engine, "start", name)
        await self.wait_ready(time.monotonic() + 300)
        entry["ready_after_seconds"] = round(time.monotonic() - began, 2)
        self.chaos_active = False
        self.restarts.append(entry)
        self.append("chaos.jsonl", entry)

    async def restart_postgres(self) -> None:
        self.chaos_active = True
        began = time.monotonic()
        entry: dict[str, Any] = {"at": round(time.time(), 1), "action": "postgres_restart"}
        result = await asyncio.to_thread(
            self.engine, "restart", "-t", "10", self.args.postgres_container
        )
        entry["restart_rc"] = result.returncode
        entry["postgres_back_seconds"] = round(time.monotonic() - began, 2)
        # Readiness reflects the database: wait for the Runtime to report ready again.
        await asyncio.sleep(1)
        await self.wait_ready(time.monotonic() + 300)
        entry["runtime_ready_after_seconds"] = round(time.monotonic() - began, 2)
        self.chaos_active = False
        self.restarts.append(entry)
        self.append("chaos.jsonl", entry)

    async def chaos(self) -> None:
        plan = [s.strip() for s in self.args.restart_pattern.split(",") if s.strip()]
        pg_at = self.began + self.args.duration * self.args.postgres_restart_at
        pg_done = self.args.postgres_restart_at <= 0
        next_restart = self.began + self.args.restart_first
        index = 0
        while not self.stopping and time.time() < self.deadline - 120:
            now = time.time()
            if not pg_done and now >= pg_at:
                pg_done = True
                await self.restart_postgres()
                continue
            if self.args.restart_every > 0 and now >= next_restart:
                await self.restart_runtime(plan[index % len(plan)])
                index += 1
                next_restart = time.time() + self.args.restart_every
                continue
            await asyncio.sleep(5)

    # --- retention ------------------------------------------------------------------------------

    def effects_now(self) -> None:
        """Read what the effect files gained since the last call."""
        for path in sorted(Path(self.args.effects_dir).glob("effects-*.jsonl")):
            offset = self.effect_offsets.get(path.name, 0)
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read()
            complete = data[: data.rfind(b"\n") + 1]
            self.effect_offsets[path.name] = offset + len(complete)
            for line in complete.decode().splitlines():
                if line.strip():
                    item = json.loads(line)
                    self.effects[item["key"]] += 1
                    self.effect_runs[item["key"]] = item["run_id"]

    async def retirement(self) -> None:
        """Rolling retention, so the database reaches a steady state as a production one does.

        Every ``--retire-every`` seconds, the runs that finished more than ``--retire-after``
        seconds ago are checked (their effects against the ledger, their approvals and the
        driver's decisions), then aged two days, as are audit events, idle conversations and
        finished workflow runs of that age. The maintenance sweep (every 30 s, windows of one
        day) must then delete them all, with their events, approvals and ledger records."""
        await asyncio.sleep(self.args.retire_every)
        while not self.stopping:
            try:
                await self.retire_once()
            except Exception as exc:  # a cycle can fail during chaos; the next one catches up
                self.append("retention.jsonl", {"at": round(time.time(), 1), "error": repr(exc)})
            await asyncio.sleep(self.args.retire_every)

    async def retire_once(self) -> None:
        after = int(self.args.retire_after)
        entry: dict[str, Any] = {
            "at": round(time.time(), 1),
            "t_s": round(time.time() - self.began),
        }
        await asyncio.to_thread(self.effects_now)
        connection = await asyncpg.connect(self.args.postgres_dsn, timeout=10)
        try:
            # The previous cycle's runs must be gone by now.
            entry["previous_still_there"] = await connection.fetchval(
                "SELECT count(*) FROM algen_agent_runtime_runs WHERE id = ANY($1::text[])",
                self.last_retired,
            )
            rows = await connection.fetch(
                "SELECT id, state->>'status' AS status FROM algen_agent_runtime_runs "
                "WHERE state->>'status' = ANY($1::text[]) AND updated_at < now() - "
                "($2 * interval '1 second') AND updated_at > now() - interval '1 day'",
                sorted(TERMINAL),
                after,
            )
            ids = [row["id"] for row in rows]
            status = {row["id"]: row["status"] for row in rows}
            workflow_rows = await connection.fetch(
                "SELECT id FROM algen_agent_runtime_workflow_checkpoints "
                "WHERE status IN ('completed', 'failed', 'cancelled') "
                "AND updated_at < now() - ($1 * interval '1 second') "
                "AND updated_at > now() - interval '1 day'",
                after,
            )
            workflow_ids = [row["id"] for row in workflow_rows]
            ledger = await connection.fetch(
                "SELECT idempotency_key, run_id, status, tool_name "
                "FROM algen_agent_runtime_tool_executions WHERE run_id = ANY($1::text[])",
                ids + workflow_ids,
            )
            approvals = await connection.fetch(
                "SELECT run_id, CASE WHEN approval->>'status' = 'pending' AND "
                "(approval->>'expires_at')::timestamptz <= now() THEN 'expired' "
                "ELSE approval->>'status' END AS status FROM algen_agent_runtime_approvals "
                "WHERE run_id = ANY($1::text[])",
                ids,
            )
            checked = self.rolling
            id_set = set(ids) | set(workflow_ids)
            records = {row["idempotency_key"]: row for row in ledger}
            for key, record in records.items():
                checked[f"ledger:{record['tool_name']}:{record['status']}"] += 1
                if record["status"] == "started":
                    checked["started_records"] += 1
                if (
                    record["tool_name"] == "soak.record"
                    and record["status"] == "completed"
                    and self.effects.get(key, 0) == 0
                ):
                    checked["completed_record_without_effect"] += 1
            for key, run_id in self.effect_runs.items():
                if run_id in id_set:
                    checked["effects"] += 1
                    if key not in records:
                        checked["effect_without_record"] += 1
                    if self.effects[key] > 1:
                        checked["duplicate_effects"] += 1
            by_run: dict[str, set[str]] = defaultdict(set)
            for row in approvals:
                by_run[row["run_id"]].add(row["status"])
                if row["status"] == "pending":
                    checked["pending_approval_on_finished_run"] += 1
            for run_id in ids:
                decision = self.decided.get(run_id)
                if decision is None:
                    continue
                checked["decisions_checked"] += 1
                if decision == "left":
                    if status[run_id] != "timed_out":
                        checked["left_but_not_timed_out"] += 1
                elif decision not in by_run.get(run_id, set()):
                    checked["decision_not_recorded"] += 1
            checked["runs"] += len(ids)
            checked["workflow_runs"] += len(workflow_ids)
            self.retired.update(ids)
            self.retired.update(workflow_ids)
            self.last_retired = ids
            entry["runs"] = len(ids)
            await connection.execute(
                "UPDATE algen_agent_runtime_runs SET updated_at = now() - interval '2 days' "
                "WHERE id = ANY($1::text[])",
                ids,
            )
            entry["audit_events"] = int(
                (
                    await connection.execute(
                        "UPDATE algen_agent_runtime_audit_events SET created_at = now() - "
                        "interval '2 days' WHERE created_at < now() - ($1 * interval '1 second') "
                        "AND created_at > now() - interval '1 day'",
                        after,
                    )
                ).split()[-1]
            )
            idle = await connection.fetch(
                "SELECT c.id FROM algen_agent_runtime_conversations c WHERE c.updated_at < now() - "
                "($1 * interval '1 second') AND c.updated_at > now() - interval '1 day' AND NOT "
                "EXISTS (SELECT 1 FROM algen_agent_runtime_conversation_messages m WHERE "
                "m.conversation_id = c.id AND (m.created_at >= now() - ($1 * interval '1 second') "
                "OR m.message->>'status' IN ('accepted', 'streaming', 'awaiting_approval', "
                "'awaiting_input', 'awaiting_credentials')))",
                after,
            )
            conversations = [row["id"] for row in idle]
            await connection.execute(
                "UPDATE algen_agent_runtime_conversation_messages SET created_at = now() - "
                "interval '2 days' WHERE conversation_id = ANY($1::text[])",
                conversations,
            )
            await connection.execute(
                "UPDATE algen_agent_runtime_conversations SET updated_at = now() - "
                "interval '2 days' WHERE id = ANY($1::text[])",
                conversations,
            )
            entry["conversations"] = len(conversations)
            await connection.execute(
                "UPDATE algen_agent_runtime_workflow_checkpoints SET updated_at = now() - "
                "interval '2 days' WHERE id = ANY($1::text[])",
                workflow_ids,
            )
            entry["workflows"] = len(workflow_ids)
        finally:
            await connection.close()
        self.retire_cycles.append(entry)
        self.append("retention.jsonl", entry)

    # --- sampling -------------------------------------------------------------------------------

    def append(self, name: str, record: dict[str, Any]) -> None:
        with (self.out / name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    async def flush_windows(self) -> None:
        while True:
            await asyncio.sleep(self.args.window_seconds)
            window, self.window = self.window, Window(time.time())
            self.append(
                "windows.jsonl",
                {
                    "t_s": round(window.started - self.began),
                    "seconds": round(time.time() - window.started, 1),
                    "noisy": self.noisy,
                    "ok": dict(window.ok),
                    "errors": dict(window.errors),
                    "latency": {
                        op: latency_summary(values) for op, values in window.latency.items()
                    },
                    "noisy_accepted": window.noisy_accepted,
                    "noisy_refused": window.noisy_refused,
                },
            )

    async def process_sample(self) -> dict[str, Any]:
        name = self.args.runtime_container
        script = (
            "grep -E '^(VmRSS|VmHWM|Threads):' /proc/1/status; echo FDS $(ls /proc/1/fd | wc -l);"
            " echo START $(stat -c %Y /proc/1); echo TICKS $(cut -d' ' -f14,15 /proc/1/stat);"
            " echo LOAD $(cut -d' ' -f1 /proc/loadavg)"
        )
        result = await asyncio.to_thread(self.engine, "exec", name, "sh", "-c", script, timeout=30)
        sample: dict[str, Any] = {}
        for line in result.stdout.splitlines():
            parts = line.replace(":", " ").split()
            if not parts:
                continue
            if parts[0] in {"VmRSS", "VmHWM"}:
                sample[parts[0].lower() + "_mb"] = round(int(parts[1]) / 1024, 1)
            elif parts[0] == "Threads":
                sample["threads"] = int(parts[1])
            elif parts[0] == "FDS":
                sample["open_fds"] = int(parts[1])
            elif parts[0] == "START":
                sample["process_started"] = int(parts[1])
            elif parts[0] == "TICKS" and len(parts) == 3:
                ticks, now = int(parts[1]) + int(parts[2]), time.monotonic()
                previous = self.cpu_ticks
                self.cpu_ticks = (sample.get("process_started"), ticks, now)
                if previous and previous[0] == sample.get("process_started") and now > previous[2]:
                    # Clock ticks are 1/100 s: the process's CPU use since the last sample.
                    used = (ticks - previous[1]) / 100 / (now - previous[2])
                    sample["process_cpu_percent"] = round(used * 100, 1)
            elif parts[0] == "LOAD":
                sample["vm_load1"] = float(parts[1])
        sample["host_load1"] = round(os.getloadavg()[0], 2)
        stats = await asyncio.to_thread(
            self.engine, "stats", "--no-stream", "--format", "{{.CPUPerc}}", name, timeout=30
        )
        with contextlib.suppress(ValueError):
            sample["cpu_percent"] = float(stats.stdout.strip().rstrip("%") or "nan")
        last = latest_probe(Path(self.args.stats_dir))
        if last:
            sample["asyncio_tasks"] = last.get("asyncio_tasks")
            sample["probe_threads"] = last.get("threads")
        return sample

    async def database_sample(self, full: bool) -> dict[str, Any]:
        connection = await asyncpg.connect(self.args.postgres_dsn, timeout=10)
        try:
            sample: dict[str, Any] = {}
            rows = await connection.fetch(
                "SELECT coalesce(state, 'none') AS state, count(*) AS n FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid() GROUP BY 1"
            )
            sample["pg_connections"] = {row["state"]: row["n"] for row in rows}
            sample["pg_db_mb"] = round(
                await connection.fetchval("SELECT pg_database_size(current_database())") / 1e6, 1
            )
            rows = await connection.fetch(
                "SELECT relname, n_live_tup, n_dead_tup, pg_total_relation_size(relid) AS bytes "
                "FROM pg_stat_user_tables WHERE relname LIKE 'algen_agent_runtime_%'"
            )
            sample["tables"] = {
                row["relname"].removeprefix("algen_agent_runtime_"): [
                    row["n_live_tup"],
                    row["n_dead_tup"],
                    round(row["bytes"] / 1e6, 2),
                ]
                for row in rows
                if row["bytes"] > 100_000
            }
            if full:
                rows = await connection.fetch(
                    "SELECT state->>'status' AS status, count(*) AS n "
                    "FROM algen_agent_runtime_runs GROUP BY 1"
                )
                sample["runs_by_status"] = {row["status"]: row["n"] for row in rows}
            return sample
        finally:
            await connection.close()

    async def redis_sample(self) -> dict[str, Any]:
        client = aioredis.from_url(self.args.redis_url)
        try:
            info = await client.info("memory")
            clients = await client.info("clients")
            return {
                "redis_used_mb": round(info["used_memory"] / 1e6, 2),
                "redis_keys": await client.dbsize(),
                "redis_clients": clients.get("connected_clients"),
            }
        finally:
            await client.aclose()

    async def sampler(self) -> None:
        count = 0
        while True:
            record: dict[str, Any] = {"t_s": round(time.time() - self.began), "noisy": self.noisy}
            for name, probe in (
                ("process", self.process_sample()),
                ("database", self.database_sample(full=count % 10 == 0)),
                ("redis", self.redis_sample()),
            ):
                try:
                    record.update(await probe)
                except Exception as exc:  # a sample can fail during chaos
                    record[f"{name}_error"] = type(exc).__name__
            self.append("samples.jsonl", record)
            count += 1
            await asyncio.sleep(self.args.sample_seconds)

    # --- invariants -----------------------------------------------------------------------------

    async def settle(self) -> dict[str, Any]:
        """After the load stops: wait until no run, turn or workflow is still working or waiting
        for an approval (left approvals expire after 90 s; the sweep runs every 30 s)."""
        connection = await asyncpg.connect(self.args.postgres_dsn, timeout=10)
        try:
            give_up = time.monotonic() + self.args.settle_seconds
            while True:
                active = await connection.fetch(
                    "SELECT state->>'status' AS status, count(*) AS n FROM "
                    "algen_agent_runtime_runs WHERE NOT (state->>'status' = ANY($1::text[])) "
                    "GROUP BY 1",
                    sorted(TERMINAL),
                )
                waiting = {row["status"]: row["n"] for row in active}
                workflows = await connection.fetchval(
                    "SELECT count(*) FROM algen_agent_runtime_workflow_checkpoints "
                    "WHERE status NOT IN ('completed', 'failed', 'cancelled')"
                )
                if (not waiting or set(waiting) <= {"awaiting_reconciliation"}) and not workflows:
                    return {"settled": True, "left": waiting}
                if time.monotonic() > give_up:
                    return {"settled": False, "left": waiting, "workflows_left": workflows}
                await asyncio.sleep(10)
        finally:
            await connection.close()

    async def swept(self, *, within: float) -> None:
        """Wait until the sweeps have deleted every aged row, or ``within`` seconds."""
        await asyncio.sleep(60)
        deadline = time.time() + within
        while time.time() < deadline:
            with contextlib.suppress(Exception):
                connection = await asyncpg.connect(self.args.postgres_dsn, timeout=10)
                try:
                    if await connection.fetchval(AGED_ROWS) == 0:
                        return
                finally:
                    await connection.close()
            await asyncio.sleep(15)

    async def invariants(self) -> dict[str, Any]:
        checks: dict[str, Any] = {}
        connection = await asyncpg.connect(self.args.postgres_dsn, timeout=30)
        try:
            # Every run the soak started is in the database (or was deleted by retention, for
            # the aged tenant), and is terminal or legitimately paused.
            journal = [
                json.loads(line)
                for line in (self.out / "journal.jsonl").read_text().splitlines()
                if line
            ]
            started_runs = {r["run_id"]: r for r in journal if r["kind"] == "run"}
            rows = await connection.fetch(
                "SELECT id, tenant_id, state->>'status' AS status, state->>'error' AS error "
                "FROM algen_agent_runtime_runs"
            )
            in_db = {row["id"]: row for row in rows}
            missing = [
                run_id
                for run_id, record in started_runs.items()
                if run_id not in in_db and run_id not in self.retired
            ]
            statuses = Counter(row["status"] for row in rows)
            not_terminal = {
                status: n
                for status, n in statuses.items()
                if status not in TERMINAL and status != "awaiting_reconciliation"
            }
            checks["runs"] = {
                "started_by_soak": len(started_runs),
                "in_database": len(in_db),
                "by_status": dict(statuses),
                "missing": len(missing),
                "missing_sample": missing[:5],
                "not_terminal": not_terminal,
                "held": not missing and not not_terminal,
            }
            # Approvals: none pending on a finished run; each decision the soak made recorded.
            rows = await connection.fetch(
                # The effective status, as the approval routes report it: a pending request
                # past its expiry reads as expired (its stored status changes only on decision).
                "SELECT a.run_id, CASE WHEN a.approval->>'status' = 'pending' AND "
                "(a.approval->>'expires_at')::timestamptz <= now() THEN 'expired' "
                "ELSE a.approval->>'status' END AS status, "
                "r.state->>'status' AS run_status FROM algen_agent_runtime_approvals a "
                "LEFT JOIN algen_agent_runtime_runs r ON r.id = a.run_id"
            )
            approval_status = Counter(row["status"] for row in rows)
            lost = [
                row["run_id"]
                for row in rows
                if row["status"] == "pending" and (row["run_status"] in TERMINAL)
            ]
            by_run: dict[str, set[str]] = defaultdict(set)
            for row in rows:
                by_run[row["run_id"]].add(row["status"])
            decisions = {
                r["run_id"]: r["decision"]
                for r in journal
                if r["kind"] == "decision" and r["decision"] != "left"
            }
            mismatched = [
                run_id
                for run_id, decision in decisions.items()
                if run_id in by_run and decision not in by_run[run_id]
            ]
            left = [
                r["run_id"] for r in journal if r["kind"] == "decision" and r["decision"] == "left"
            ]
            left_not_expired = [
                run_id
                for run_id in left
                if run_id in in_db and in_db[run_id]["status"] != "timed_out"
            ]
            checks["approvals"] = {
                "by_status": dict(approval_status),
                "pending_on_finished_run": len(lost),
                "decisions_made": len(decisions),
                "decisions_not_recorded": len(mismatched),
                "decisions_not_recorded_sample": mismatched[:5],
                "left_to_expire": len(left),
                "left_but_not_timed_out": len(left_not_expired),
                "left_but_not_timed_out_sample": left_not_expired[:5],
                "held": not lost and not mismatched and not left_not_expired,
            }
            # The ledger against the effects: one ledger record per effect, no duplicate effect.
            effects, effect_runs = read_effects(Path(self.args.effects_dir))
            rows = await connection.fetch(
                "SELECT idempotency_key, status, tenant_id FROM algen_agent_runtime_tool_executions "
                "WHERE tool_name = 'soak.record'"
            )
            ledger_status = Counter(row["status"] for row in rows)
            ledger = {row["idempotency_key"]: row["status"] for row in rows}
            duplicates = [key for key, n in effects.items() if n > 1]
            effect_without_ledger = [
                key for key in effects if key not in ledger and effect_runs[key] not in self.retired
            ]
            completed_without_effect = [
                key
                for key, status in ledger.items()
                if status == "completed" and key not in effects
            ]
            effect_not_completed = Counter(
                ledger[key] for key in effects if key in ledger and ledger[key] != "completed"
            )
            stuck = await connection.fetchval(
                "SELECT count(*) FROM algen_agent_runtime_tool_executions WHERE status = 'started'"
            )
            all_ledger = await connection.fetch(
                "SELECT tool_name, status, count(*) AS n FROM "
                "algen_agent_runtime_tool_executions GROUP BY 1, 2"
            )
            checks["ledger"] = {
                "effects": sum(effects.values()),
                "distinct_effect_keys": len(effects),
                "duplicate_effects": len(duplicates),
                "duplicate_sample": duplicates[:5],
                "record_ledger_by_status": dict(ledger_status),
                "all_ledger": {f"{r['tool_name']}:{r['status']}": r["n"] for r in all_ledger},
                "effect_without_ledger_record": len(effect_without_ledger),
                "effect_without_ledger_sample": effect_without_ledger[:5],
                "completed_record_without_effect": len(completed_without_effect),
                "completed_without_effect_sample": completed_without_effect[:5],
                "effects_whose_record_is_not_completed": dict(effect_not_completed),
                "started_records_left": stuck,
                "held": not duplicates
                and not effect_without_ledger
                and not completed_without_effect
                and stuck == 0,
            }
            # No stuck turns or workflows.
            rows = await connection.fetch(
                "SELECT message->>'role' AS role, message->>'status' AS status, count(*) AS n "
                "FROM algen_agent_runtime_conversation_messages GROUP BY 1, 2"
            )
            messages = {f"{row['role']}:{row['status']}": row["n"] for row in rows}
            stuck_turns = {
                key: n
                for key, n in messages.items()
                if key.split(":")[1]
                in {"accepted", "streaming", "awaiting_approval", "awaiting_input"}
            }
            rows = await connection.fetch(
                "SELECT status, count(*) AS n FROM algen_agent_runtime_workflow_checkpoints "
                "GROUP BY 1"
            )
            workflows = {row["status"]: row["n"] for row in rows}
            stuck_workflows = {
                k: v for k, v in workflows.items() if k not in {"completed", "failed", "cancelled"}
            }
            checks["stuck"] = {
                "messages": messages,
                "stuck_turns": stuck_turns,
                "workflows": workflows,
                "stuck_workflows": stuck_workflows,
                "held": not stuck_turns and not stuck_workflows,
            }
            # Retention: aged runs deleted with their events, approvals and ledger records.
            leftovers = await connection.fetchval(AGED_ROWS)
            orphan_events = await connection.fetchval(
                "SELECT count(*) FROM algen_agent_runtime_events e WHERE NOT EXISTS "
                "(SELECT 1 FROM algen_agent_runtime_runs r WHERE r.id = e.run_id)"
            )
            expired_artifacts = await connection.fetchval(
                "SELECT count(*) FROM algen_agent_runtime_artifacts WHERE expires_at < now() - "
                "interval '10 minutes'"
            )
            rolling = dict(self.rolling)
            checks["retention"] = {
                "cycles": len(self.retire_cycles),
                "retired": {
                    key: sum(int(c.get(key, 0)) for c in self.retire_cycles)
                    for key in ("runs", "workflows", "conversations", "audit_events")
                },
                "rolling_checks": rolling,
                "cycles_with_previous_runs_left": [
                    c for c in self.retire_cycles if c.get("previous_still_there")
                ],
                "cycle_errors": [c for c in self.retire_cycles if "error" in c],
                "aged_rows_left_now": leftovers,
                "events_without_run": orphan_events,
                "artifacts_expired_over_10_min_ago_still_stored": expired_artifacts,
                "held": leftovers == 0
                and expired_artifacts == 0
                and not any(c.get("previous_still_there") for c in self.retire_cycles)
                and all(
                    rolling.get(key, 0) == 0
                    for key in (
                        "started_records",
                        "completed_record_without_effect",
                        "effect_without_record",
                        "duplicate_effects",
                        "pending_approval_on_finished_run",
                        "left_but_not_timed_out",
                        "decision_not_recorded",
                    )
                ),
            }
        finally:
            await connection.close()
        return checks

    # --- main -----------------------------------------------------------------------------------

    async def run(self) -> dict[str, Any]:
        background = [
            asyncio.create_task(self.flush_windows()),
            asyncio.create_task(self.sampler()),
        ]
        load = [
            asyncio.create_task(self.worker(tenant, f"{tenant}-u{index % 3}", index))
            for tenant in self.args.tenants
            for index in range(self.args.workers)
        ]
        load += [
            asyncio.create_task(self.conversation_worker(tenant, f"{tenant}-chat{index}"))
            for tenant in self.args.tenants
            for index in range(self.args.chat_workers)
        ]
        load += [asyncio.create_task(self.noisy_client()) for _ in range(self.args.noisy_clients)]
        load += [
            asyncio.create_task(self.noisy_schedule()),
            asyncio.create_task(self.chaos()),
            asyncio.create_task(self.retirement()),
        ]
        print(f"soak: {len(load)} tasks for {self.args.duration} s", flush=True)
        while time.time() < self.deadline:
            await asyncio.sleep(min(60, max(1, self.deadline - time.time())))
            elapsed = round((time.time() - self.began) / 60, 1)
            print(
                f"soak: {elapsed} min ok={sum(self.totals_ok.values())} "
                f"errors={sum(self.totals_errors.values())} noisy={self.noisy}",
                flush=True,
            )
        self.stopping = True
        self.noisy = False
        print("soak: load stopped; letting work in flight finish", flush=True)
        _, pending = await asyncio.wait(load, timeout=self.args.drain_seconds)
        for task in pending:
            task.cancel()
        settle = await self.settle()
        print(f"soak: settled {settle}", flush=True)
        # A last retirement, and time for the sweeps to delete it: they delete at most 20,000
        # audit events each, every 30 s, so a cycle's worth can take a few sweeps.
        with contextlib.suppress(Exception):
            await self.retire_once()
        await self.swept(within=600)
        checks = await self.invariants()
        for task in background:
            task.cancel()
        summary = {
            "duration_s": round(time.time() - self.began),
            "load_seconds": self.args.duration,
            "operations_ok": dict(self.totals_ok),
            "errors": dict(sorted(self.totals_errors.items())),
            "outcomes": dict(sorted(self.outcomes.items())),
            "noisy": dict(self.noisy_totals),
            "latency": {op: latency_summary(v) for op, v in sorted(self.all_latency.items())},
            "latency_quiet": {
                op: latency_summary(v) for op, v in sorted(self.quiet_latency.items())
            },
            "latency_noisy": {
                op: latency_summary(v) for op, v in sorted(self.noisy_latency.items())
            },
            "chaos": self.restarts,
            "settle": settle,
            "invariants": checks,
            "held": all(check.get("held") for check in checks.values()),
            "unfinished_tasks_cancelled": len(pending),
        }
        (self.out / "summary.json").write_text(json.dumps(summary, indent=2))
        await self.client.aclose()
        self.journal.close()
        return summary


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add = parser.add_argument
    add("--base-url", required=True)
    add("--private-key", required=True, help="PEM file of the RS256 key that signs the tokens")
    add("--postgres-dsn", required=True)
    add("--redis-url", required=True)
    add("--engine", default="podman")
    add("--runtime-container", required=True)
    add("--postgres-container", required=True)
    add("--effects-dir", required=True)
    add("--stats-dir", required=True)
    add("--out", required=True)
    add("--duration", type=float, default=3 * 3600, help="seconds of load")
    add("--tenants", nargs="+", default=["t-alpha", "t-beta", "t-gamma"])
    add("--workers", type=int, default=6, help="mixed-work clients per tenant")
    add("--chat-workers", type=int, default=2, help="conversation clients per tenant")
    add("--think", type=float, nargs=2, default=(0.5, 2.0), help="pause between operations, s")
    add("--noisy-clients", type=int, default=60)
    add("--noisy-first", type=float, default=900)
    add("--noisy-period", type=float, default=2400)
    add("--noisy-length", type=float, default=600)
    add("--leave-approval-fraction", type=float, default=0.15)
    add("--restart-first", type=float, default=1200)
    add("--restart-every", type=float, default=1500)
    add("--restart-pattern", default="TERM,TERM,KILL")
    # Above shutdown_grace_seconds x 3 plus a margin, as docs/operations.md advises.
    add("--stop-timeout", type=int, default=45)
    add("--postgres-restart-at", type=float, default=0.55, help="fraction of the duration")
    add("--retire-every", type=float, default=300)
    add("--retire-after", type=float, default=300)
    add("--window-seconds", type=float, default=300)
    add("--sample-seconds", type=float, default=30)
    add("--drain-seconds", type=float, default=300)
    add("--settle-seconds", type=float, default=600)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse(sys.argv[1:] if argv is None else argv)
    summary = asyncio.run(Soak(args).run())
    print("SOAK " + json.dumps({k: summary[k] for k in ("duration_s", "held")}), flush=True)
    print(json.dumps(summary["invariants"], indent=2)[:6000], flush=True)
    return 0 if summary["held"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
