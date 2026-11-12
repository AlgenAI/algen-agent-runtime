from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from algen_agent_runtime.exceptions.errors import (
    ConflictError,
    QuotaExceededError,
    RunConflictError,
    StoreUnavailableError,
)
from algen_agent_runtime.persistence.quotas import ResourceUsage, StorageQuotas
from algen_agent_runtime.types.contracts import Message, RunState

# redis-py's errors for a server it cannot reach or that did not answer in time (redis is
# optional, so they are recognised by name).
_UNAVAILABLE_ERRORS = frozenset({"ConnectionError", "TimeoutError", "BusyLoadingError"})


def redis_unavailable(exc: BaseException) -> StoreUnavailableError | None:
    """The runtime error for a redis-py error that means Redis is unavailable, else None."""
    if isinstance(exc, StoreUnavailableError):
        return None
    names = {cls.__name__ for cls in type(exc).__mro__ if cls.__module__.startswith("redis")}
    if names & _UNAVAILABLE_ERRORS:
        return StoreUnavailableError(f"Redis is unavailable ({type(exc).__name__})")
    return None


@asynccontextmanager
async def _available() -> AsyncIterator[None]:
    try:
        yield
    except Exception as exc:
        unavailable = redis_unavailable(exc)
        if unavailable is not None:
            raise unavailable from exc
        raise


# Session memory with usage accounting, atomically. KEYS: the session list, the tenant's usage
# hash (items, bytes), a sorted set of the tenant's sessions by expiry time, and a hash of each
# session's items and bytes. Sessions whose list has expired are first subtracted from the usage.
# ARGV: ttl ms, item limit, byte limit (-1 for none), items and bytes to add, then the messages.
_APPEND_MEMORY = """
local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local ttl = tonumber(ARGV[1])
local max_items = tonumber(ARGV[2])
local max_bytes = tonumber(ARGV[3])
local add_items = tonumber(ARGV[4])
local add_bytes = tonumber(ARGV[5])
local expired = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', now, 'LIMIT', 0, 256)
for _, session in ipairs(expired) do
  local size = redis.call('HMGET', KEYS[4], session .. '|items', session .. '|bytes')
  redis.call('HINCRBY', KEYS[2], 'items', -(tonumber(size[1]) or 0))
  redis.call('HINCRBY', KEYS[2], 'bytes', -(tonumber(size[2]) or 0))
  redis.call('HDEL', KEYS[4], session .. '|items', session .. '|bytes')
  redis.call('ZREM', KEYS[3], session)
end
local items = tonumber(redis.call('HGET', KEYS[2], 'items') or '0')
local bytes = tonumber(redis.call('HGET', KEYS[2], 'bytes') or '0')
if add_items == 0 then
  return {1, items, bytes}
end
if max_items >= 0 and items + add_items > max_items then
  return {0, 'items', items + add_items}
end
if max_bytes >= 0 and bytes + add_bytes > max_bytes then
  return {0, 'bytes', bytes + add_bytes}
end
for i = 6, #ARGV do
  redis.call('RPUSH', KEYS[1], ARGV[i])
end
redis.call('PEXPIRE', KEYS[1], ttl)
redis.call('HINCRBY', KEYS[2], 'items', add_items)
redis.call('HINCRBY', KEYS[2], 'bytes', add_bytes)
redis.call('HINCRBY', KEYS[4], KEYS[1] .. '|items', add_items)
redis.call('HINCRBY', KEYS[4], KEYS[1] .. '|bytes', add_bytes)
redis.call('ZADD', KEYS[3], now + ttl, KEYS[1])
return {1, items + add_items, bytes + add_bytes}
"""

_DELETE_MEMORY = """
local size = redis.call('HMGET', KEYS[4], KEYS[1] .. '|items', KEYS[1] .. '|bytes')
if size[1] then
  redis.call('HINCRBY', KEYS[2], 'items', -tonumber(size[1]))
  redis.call('HINCRBY', KEYS[2], 'bytes', -(tonumber(size[2]) or 0))
  redis.call('HDEL', KEYS[4], KEYS[1] .. '|items', KEYS[1] .. '|bytes')
  redis.call('ZREM', KEYS[3], KEYS[1])
end
return redis.call('DEL', KEYS[1])
"""


def _glob_escape(value: str) -> str:
    """``value`` matched literally in a Redis SCAN MATCH pattern."""
    return "".join("\\" + character if character in "*?[]\\" else character for character in value)


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class RedisRunStore:
    def __init__(self, redis: Any, prefix: str = "algen-agent-runtime") -> None:
        self._redis = redis
        self._prefix = prefix

    def _key(self, tenant_id: str, run_id: str) -> str:
        return f"{self._prefix}:tenant:{tenant_id}:run:{run_id}"

    def _turn_key(self, tenant_id: str, conversation_id: str, turn_id: str) -> str:
        # Not matched by list_active's ``*:run:*`` pattern.
        return f"{self._prefix}:tenant:{tenant_id}:turn-run:{conversation_id}:{turn_id}"

    async def create(self, state: RunState) -> None:
        async with _available():
            created = await self._redis.set(
                self._key(state.request.tenant_id, state.id), state.model_dump_json(), nx=True
            )
            if not created:
                raise ConflictError(f"run {state.id!r} already exists")
            request = state.request
            if request.conversation_id and request.turn_id:
                # The turn's latest run, so a restarted conversation service finds it.
                await self._redis.set(
                    self._turn_key(request.tenant_id, request.conversation_id, request.turn_id),
                    state.id,
                )

    async def find_by_turn(
        self, tenant_id: str, conversation_id: str, turn_id: str
    ) -> RunState | None:
        """The latest run started for a conversation turn, ended or not, or None.

        Runs created before this index existed are found by scanning the tenant's runs.
        """
        async with _available():
            run_id = await self._redis.get(self._turn_key(tenant_id, conversation_id, turn_id))
            if run_id:
                value = await self._redis.get(self._key(tenant_id, _text(run_id)))
                return RunState.model_validate_json(value) if value else None
            latest: RunState | None = None
            pattern = f"{self._prefix}:tenant:{tenant_id}:run:*"
            async for key in self._redis.scan_iter(match=pattern, count=1000):
                value = await self._redis.get(key)
                if not value:
                    continue
                state = RunState.model_validate_json(value)
                if (
                    state.request.conversation_id == conversation_id
                    and state.request.turn_id == turn_id
                    and (latest is None or state.updated_at > latest.updated_at)
                ):
                    latest = state
            return latest

    async def get(self, run_id: str, tenant_id: str) -> RunState | None:
        async with _available():
            value = await self._redis.get(self._key(tenant_id, run_id))
            return RunState.model_validate_json(value) if value else None

    async def save(self, state: RunState, expected_version: int) -> None:
        async with _available():
            key = self._key(state.request.tenant_id, state.id)
            async with self._redis.pipeline(transaction=True) as pipe:
                await pipe.watch(key)
                value = await pipe.get(key)
                current = RunState.model_validate_json(value) if value else None
                if current is None or current.version != expected_version:
                    raise RunConflictError(f"run {state.id!r} was concurrently modified")
                saved = state.model_copy(update={"version": expected_version + 1})
                pipe.multi()
                pipe.set(key, saved.model_dump_json())
                try:
                    await pipe.execute()
                except Exception as exc:
                    # redis-py raises WatchError when another writer touched the key after WATCH.
                    if type(exc).__name__ == "WatchError":
                        raise RunConflictError(
                            f"run {state.id!r} was concurrently modified"
                        ) from exc
                    raise
                state.version = saved.version

    async def list_active(self, limit: int = 1000) -> Sequence[RunState]:
        async with _available():
            from algen_agent_runtime.types.contracts import TERMINAL_STATUSES

            states: list[RunState] = []
            pattern = f"{self._prefix}:tenant:*:run:*"
            async for key in self._redis.scan_iter(match=pattern, count=min(limit, 1000)):
                value = await self._redis.get(key)
                if value:
                    state = RunState.model_validate_json(value)
                    if state.status not in TERMINAL_STATUSES:
                        states.append(state)
                        if len(states) >= limit:
                            break
            states.sort(key=lambda state: state.updated_at)
            return tuple(states)

    async def list_for_user(self, tenant_id: str, user_id: str) -> Sequence[RunState]:
        """Every run of ``user_id`` in the tenant, oldest first. Scans the tenant's run keys."""
        async with _available():
            states: list[RunState] = []
            pattern = f"{_glob_escape(self._prefix)}:tenant:{_glob_escape(tenant_id)}:run:*"
            async for key in self._redis.scan_iter(match=pattern, count=1000):
                value = await self._redis.get(key)
                if value:
                    state = RunState.model_validate_json(value)
                    if state.request.tenant_id == tenant_id and state.request.user_id == user_id:
                        states.append(state)
            states.sort(key=lambda state: state.started_at)
            return tuple(states)

    async def delete_many(self, tenant_id: str, run_ids: Sequence[str]) -> dict[str, int]:
        """Delete the tenant's runs among ``run_ids``; return ``{"runs": count}``."""
        keys = [self._key(tenant_id, run_id) for run_id in dict.fromkeys(run_ids)]
        if not keys:
            return {"runs": 0}
        async with _available():
            return {"runs": int(await self._redis.delete(*keys))}


class RedisMemoryStore:
    """Session memory lists with a TTL.

    With ``quotas``, appends and deletes run as Lua scripts that keep each tenant's usage beside
    its sessions, so a quota is checked atomically; the scripts touch several keys, so this needs
    a Redis that is not clustered. Sessions written before quotas were configured are not
    counted until they expire.
    """

    def __init__(
        self,
        redis: Any,
        prefix: str = "algen-agent-runtime",
        retention_seconds: int = 86400,
        quotas: StorageQuotas | None = None,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._retention = retention_seconds
        self._quotas = quotas

    def _key(self, tenant_id: str, session_id: str) -> str:
        return f"{self._prefix}:tenant:{tenant_id}:session:{session_id}:memory"

    def _usage_keys(self, tenant_id: str) -> tuple[str, str, str]:
        usage = f"{self._prefix}:tenant:{tenant_id}:usage:memory"
        return usage, f"{usage}:sessions", f"{usage}:sizes"

    def _owner_key(self, tenant_id: str, session_id: str) -> str:
        return f"{self._prefix}:tenant:{tenant_id}:session:{session_id}:owner"

    async def claim_session(self, tenant_id: str, session_id: str, user_id: str) -> bool:
        """Bind the session to ``user_id`` on first use; False when another user owns it.

        The claim has the memory's TTL and is renewed by each claim of its owner, which the
        Runtime makes before it appends, so it outlives the session's messages.
        """
        key = self._owner_key(tenant_id, session_id)
        ttl = max(1, self._retention)
        async with _available():
            # Twice: a claim that expires between SET NX and GET is taken on the second pass.
            for _ in range(2):
                if await self._redis.set(key, user_id, nx=True, ex=ttl):
                    return True
                owner = await self._redis.get(key)
                if owner is None:
                    continue
                if _text(owner) != user_id:
                    return False
                await self._redis.expire(key, ttl)
                return True
        return False

    async def append(self, tenant_id: str, session_id: str, messages: Sequence[Message]) -> None:
        async with _available():
            key = self._key(tenant_id, session_id)
            if not messages:
                return
            values = [message.model_dump_json() for message in messages]
            if self._quotas is None:
                await self._redis.rpush(key, *values)
                await self._redis.expire(key, self._retention)
                return
            limit = self._quotas.limit(tenant_id, "memory")
            result = await self._redis.eval(
                _APPEND_MEMORY,
                4,
                key,
                *self._usage_keys(tenant_id),
                max(1, self._retention) * 1000,
                -1 if limit.items is None else limit.items,
                -1 if limit.bytes is None else limit.bytes,
                len(values),
                sum(len(value.encode()) for value in values),
                *values,
            )
            if int(result[0]) == 0:
                dimension = _text(result[1])
                maximum = limit.items if dimension == "items" else limit.bytes
                raise QuotaExceededError("memory", dimension, int(maximum or 0), int(result[2]))

    async def tenant_usage(self, tenant_id: str) -> ResourceUsage:
        """Usage of the tenant's unexpired sessions; zero without ``quotas``."""
        if self._quotas is None:
            return ResourceUsage()
        async with _available():
            result = await self._redis.eval(
                _APPEND_MEMORY, 4, "", *self._usage_keys(tenant_id), 0, -1, -1, 0, 0
            )
        return ResourceUsage(int(result[1]), int(result[2]))

    async def get(self, tenant_id: str, session_id: str, limit: int) -> Sequence[Message]:
        async with _available():
            values = await self._redis.lrange(self._key(tenant_id, session_id), -limit, -1)
            return tuple(Message.model_validate_json(value) for value in values)

    async def delete(self, tenant_id: str, session_id: str) -> None:
        async with _available():
            key = self._key(tenant_id, session_id)
            if self._quotas is None:
                await self._redis.delete(key)
                return
            await self._redis.eval(_DELETE_MEMORY, 4, key, *self._usage_keys(tenant_id))

    async def delete_session(self, tenant_id: str, session_id: str) -> int:
        """Delete the session's memory and its owner; return how many messages went."""
        async with _available():
            count = int(await self._redis.llen(self._key(tenant_id, session_id)))
        await self.delete(tenant_id, session_id)
        async with _available():
            await self._redis.delete(self._owner_key(tenant_id, session_id))
        return count

    async def session_owner(self, tenant_id: str, session_id: str) -> str | None:
        async with _available():
            owner = await self._redis.get(self._owner_key(tenant_id, session_id))
        return None if owner is None else _text(owner)

    async def sessions_for_user(self, tenant_id: str, user_id: str) -> Sequence[str]:
        """The sessions ``user_id`` owns in the tenant. Scans the tenant's owner keys."""
        head = f"{self._prefix}:tenant:{tenant_id}:session:"
        pattern = f"{_glob_escape(head)}*:owner"
        sessions: list[str] = []
        async with _available():
            async for key in self._redis.scan_iter(match=pattern, count=1000):
                name = _text(key)
                owner = await self._redis.get(key)
                if owner is not None and _text(owner) == user_id:
                    sessions.append(name[len(head) : -len(":owner")])
        return tuple(sorted(sessions))
