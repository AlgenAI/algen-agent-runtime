"""Per-tenant storage quotas for artifacts, session memory and learned memory.

A quota bounds a tenant's stored items and bytes per resource. Every store tracks usage; a write
is checked only when the tenant has a limit for that resource. A write that would take usage past
a limit raises :class:`QuotaExceededError` and writes nothing; deletes and writes that shrink usage
are always allowed, so a tenant over its quota (after the quota was lowered) can clean up.

Bytes are the stored size: artifact payload bytes, and the JSON size of a memory message or a
learned-memory record (PostgreSQL measures its ``jsonb`` text, so the two backends differ by a few
bytes per record). Expired records count until the maintenance sweep purges them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.exceptions.errors import QuotaExceededError

QuotaResource = Literal["artifacts", "memory", "learned_memory"]
QUOTA_RESOURCES: tuple[QuotaResource, ...] = ("artifacts", "memory", "learned_memory")


class StorageQuota(BaseModel):
    """Limits for one tenant; ``None`` is unlimited."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_count: int | None = Field(default=None, ge=0)
    artifact_bytes: int | None = Field(default=None, ge=0)
    memory_items: int | None = Field(default=None, ge=0, description="Session memory messages.")
    memory_bytes: int | None = Field(default=None, ge=0)
    learned_memory_items: int | None = Field(
        default=None, ge=0, description="Learned-memory records that hold content."
    )
    learned_memory_bytes: int | None = Field(default=None, ge=0)

    def limit(self, resource: QuotaResource) -> ResourceLimit:
        if resource == "artifacts":
            return ResourceLimit(self.artifact_count, self.artifact_bytes)
        if resource == "memory":
            return ResourceLimit(self.memory_items, self.memory_bytes)
        return ResourceLimit(self.learned_memory_items, self.learned_memory_bytes)


class StorageQuotaSettings(BaseModel):
    """``default`` applies to every tenant; a ``tenants`` entry overrides the fields it sets.

    Set a field to ``null`` in a tenant's entry to lift that limit for the tenant.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    default: StorageQuota = Field(default_factory=StorageQuota)
    tenants: dict[str, StorageQuota] = Field(default_factory=dict)


class ResourceLimit(NamedTuple):
    items: int | None
    bytes: int | None

    @property
    def unlimited(self) -> bool:
        return self.items is None and self.bytes is None


class ResourceUsage(NamedTuple):
    items: int = 0
    bytes: int = 0


class StorageQuotas:
    """Resolves each tenant's limits and checks a write's effect on usage against them."""

    def __init__(self, settings: StorageQuotaSettings | None = None) -> None:
        settings = settings or StorageQuotaSettings()
        self._default = settings.default
        self._tenants = {
            tenant: settings.default.model_copy(
                update={name: getattr(quota, name) for name in quota.model_fields_set}
            )
            for tenant, quota in settings.tenants.items()
        }

    @property
    def configured(self) -> bool:
        """Whether any tenant has any limit."""
        return any(
            value is not None
            for quota in (self._default, *self._tenants.values())
            for value in quota.model_dump().values()
        )

    def enforced(self, tenant_id: str, resource: QuotaResource) -> bool:
        return not self.limit(tenant_id, resource).unlimited

    def for_tenant(self, tenant_id: str) -> StorageQuota:
        return self._tenants.get(tenant_id, self._default)

    def limit(self, tenant_id: str, resource: QuotaResource) -> ResourceLimit:
        return self.for_tenant(tenant_id).limit(resource)

    def check(
        self,
        tenant_id: str,
        resource: QuotaResource,
        before: ResourceUsage,
        after: ResourceUsage,
    ) -> None:
        """Raise when ``after`` passes a limit on a dimension the write made larger."""
        limit = self.limit(tenant_id, resource)
        for dimension, maximum, old, new in (
            ("items", limit.items, before.items, after.items),
            ("bytes", limit.bytes, before.bytes, after.bytes),
        ):
            if maximum is not None and new > maximum and new > old:
                raise QuotaExceededError(resource, dimension, maximum, new)


class TenantResourceUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    items: int
    bytes: int
    items_limit: int | None = None
    bytes_limit: int | None = None


class TenantStorageUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: str
    resources: dict[str, TenantResourceUsage]


UsageReader = Callable[[str], Awaitable[ResourceUsage]]


class StorageUsageService:
    """A tenant's stored items and bytes per resource, with the limits that apply to it."""

    def __init__(self, quotas: StorageQuotas, readers: Mapping[QuotaResource, UsageReader]) -> None:
        self._quotas = quotas
        self._readers = dict(readers)

    async def usage(self, tenant_id: str) -> TenantStorageUsage:
        resources: dict[str, TenantResourceUsage] = {}
        for resource, reader in self._readers.items():
            used = await reader(tenant_id)
            limit = self._quotas.limit(tenant_id, resource)
            resources[resource] = TenantResourceUsage(
                items=used.items, bytes=used.bytes, items_limit=limit.items, bytes_limit=limit.bytes
            )
        return TenantStorageUsage(tenant_id=tenant_id, resources=resources)


# PostgreSQL: usage rows are maintained by the triggers of migration 016.

_ENSURE_USAGE = (
    "INSERT INTO algen_agent_runtime_tenant_usage (tenant_id, resource) VALUES ($1, $2) "
    "ON CONFLICT (tenant_id, resource) DO NOTHING"
)
_SELECT_USAGE = (
    "SELECT items, bytes FROM algen_agent_runtime_tenant_usage WHERE tenant_id=$1 AND resource=$2"
)


async def read_usage(connection: Any, tenant_id: str, resource: QuotaResource) -> ResourceUsage:
    row = await connection.fetchrow(_SELECT_USAGE, tenant_id, resource)
    return ResourceUsage(int(row["items"]), int(row["bytes"])) if row else ResourceUsage()


async def lock_usage(connection: Any, tenant_id: str, resource: QuotaResource) -> ResourceUsage:
    """Lock the tenant's usage row for this transaction and return it.

    Every other quota-checked write, and every trigger update of the row, waits for this
    transaction to end, so the usage read after the write includes all committed changes.
    """
    await connection.execute(_ENSURE_USAGE, tenant_id, resource)
    row = await connection.fetchrow(_SELECT_USAGE + " FOR UPDATE", tenant_id, resource)
    return ResourceUsage(int(row["items"]), int(row["bytes"]))


async def postgres_quota_write[T](
    database: Any,
    quotas: StorageQuotas | None,
    tenant_id: str,
    resource: QuotaResource,
    write: Callable[[Any], Awaitable[T]],
) -> T:
    """Run ``write(connection)`` and roll it back if it takes the tenant past its quota.

    Without a limit for the tenant and resource, ``write`` runs on ``database`` directly.
    """
    if quotas is None or not quotas.enforced(tenant_id, resource):
        return await write(database)
    async with database.transaction() as connection:
        before = await lock_usage(connection, tenant_id, resource)
        result = await write(connection)
        quotas.check(tenant_id, resource, before, await read_usage(connection, tenant_id, resource))
        return result
