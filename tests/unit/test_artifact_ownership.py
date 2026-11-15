"""An artifact belongs to a user of its tenant: another user cannot list, read, delete or
attach it (DATA-1 / API-4)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
from httpx import ASGITransport

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.artifacts import (
    ARTIFACTS_ADMIN_SCOPE,
    artifact_owned_by,
    list_user_artifacts,
)
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.exceptions.errors import CapabilityError
from algen_agent_runtime.persistence import UserDataService
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.persistence.s3 import S3ArtifactStore
from algen_agent_runtime.types.contracts import (
    Artifact,
    ArtifactDescriptor,
    ArtifactStatus,
    RunAttachment,
    RunRequest,
    RunState,
    RunStatus,
)

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x02\x00\x00\x00\x02\x08\x02\x00\x00\x00"
    b"\xfd\xd4\x9as\x00\x00\x00\x00IEND\xaeB`\x82"
)
SCOPES = "runs:read runs:write conversations:read conversations:write"


def _headers(user: str, scopes: str = SCOPES, tenant: str = "acme") -> dict[str, str]:
    return {"x-tenant-id": tenant, "x-user-id": user, "x-scopes": scopes}


ALICE = _headers("alice")
BOB = _headers("bob")
ADMIN = _headers("operator", f"runs:read runs:write {ARTIFACTS_ADMIN_SCOPE}")


def _settings() -> AppSettings:
    return AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
            "agents": [
                {
                    "name": "reader",
                    "version": "1.0.0",
                    "description": "Reads documents",
                    "system_instructions": "Say what the document is.",
                    "default_model": {"name": "d", "provider": "mock", "model": "deterministic"},
                    "attachments": {"enabled": True},
                }
            ],
            "security": {"upload_scanner": "basic"},
            "runtime": {"recover_incomplete_runs": False},
        }
    )


async def _upload(client: httpx.AsyncClient, headers: dict[str, str], **query: str) -> Any:
    params = {"name": "passport.png", "media_type": "image/png", **query}
    response = await client.post("/v1/artifacts", params=params, headers=headers, content=PNG)
    assert response.status_code == 201, response.text
    return response.json()


async def _finished(client: httpx.AsyncClient, run_id: str, headers: dict[str, str]) -> Any:
    for _ in range(300):
        run = (await client.get(f"/v1/runs/{run_id}", headers=headers)).json()
        if run["status"] in {"completed", "failed"}:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError("run did not finish")


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# --- stores --------------------------------------------------------------------------------


async def test_the_memory_store_keeps_the_owner_and_lists_a_users_artifacts() -> None:
    store = InMemoryArtifactStore()
    mine = Artifact(tenant_id="t", user_id="alice", name="a", media_type="text/plain", data=b"a")
    theirs = Artifact(tenant_id="t", user_id="bob", name="b", media_type="text/plain", data=b"b")
    legacy = Artifact(tenant_id="t", name="c", media_type="text/plain", data=b"c")
    elsewhere = Artifact(
        tenant_id="t2", user_id="alice", name="d", media_type="text/plain", data=b"d"
    )
    for artifact in (mine, theirs, legacy, elsewhere):
        await store.put(artifact)

    described = await store.describe(mine.id, "t")
    assert described is not None and described.user_id == "alice"
    assert [item.id for item in await store.list_for_user("t", "alice")] == [mine.id]
    assert await store.list_for_user("t", "alice", status=ArtifactStatus.QUARANTINED) == ()
    assert len(await store.list("t")) == 3  # the tenant listing is unchanged


async def test_the_s3_store_records_the_owner_and_lists_by_it() -> None:
    class Client:
        def put_object(self, **kwargs: Any) -> None:
            return None

    database = AsyncMock()
    database.fetch.return_value = []
    store = S3ArtifactStore(database, bucket="b", client=Client())

    await store.put(Artifact(tenant_id="t", user_id="alice", name="a", media_type="x/y", data=b"a"))
    insert = database.execute.await_args
    assert "user_id" in insert.args[0] and insert.args[-1] == "alice"

    await store.list_for_user("t", "alice")
    query = database.fetch.await_args
    assert "user_id=$2" in query.args[0] and query.args[1:3] == ("t", "alice")


async def test_list_user_artifacts_filters_a_store_without_list_for_user() -> None:
    class TenantOnlyStore:
        def __init__(self, items: Sequence[ArtifactDescriptor]) -> None:
            self.items = items

        async def list(self, tenant_id: str, **_: Any) -> Sequence[ArtifactDescriptor]:
            return self.items

    owned = Artifact(tenant_id="t", user_id="alice", name="a", media_type="x/y", data=b"a")
    other = Artifact(tenant_id="t", user_id="bob", name="b", media_type="x/y", data=b"b")
    store = TenantOnlyStore([owned.descriptor(), other.descriptor()])

    listed = await list_user_artifacts(store, "t", "alice")

    assert [item.id for item in listed] == [owned.id]


def test_artifact_owned_by() -> None:
    owned = Artifact(
        tenant_id="t", user_id="alice", run_id="r", name="a", media_type="x/y", data=b""
    )
    legacy_run = Artifact(tenant_id="t", run_id="r", name="a", media_type="x/y", data=b"")
    legacy = Artifact(tenant_id="t", name="a", media_type="x/y", data=b"")

    assert artifact_owned_by(owned, "alice")
    assert not artifact_owned_by(owned, "bob")
    assert artifact_owned_by(owned, "bob", run_user_id="bob")  # the user of its run
    assert artifact_owned_by(legacy_run, "alice", run_user_id="alice")
    assert not artifact_owned_by(legacy_run, "bob", run_user_id="alice")
    assert not artifact_owned_by(legacy, "alice", run_user_id="alice")  # no run: admin only
    assert not artifact_owned_by(legacy, "")


# --- routes --------------------------------------------------------------------------------


async def test_another_user_of_the_tenant_cannot_see_read_or_delete_an_upload() -> None:
    app = create_app(_settings())
    async with _client(app) as client:
        upload = await _upload(client, ALICE)
        assert upload["user_id"] == "alice"
        path = f"/v1/artifacts/{upload['id']}"

        assert (await client.get(path, headers=BOB)).status_code == 404
        assert (await client.get(f"{path}/metadata", headers=BOB)).status_code == 404
        assert (await client.delete(path, headers=BOB)).status_code == 404
        assert (await client.get("/v1/artifacts", headers=BOB)).json() == []

        assert (await client.get(path, headers=ALICE)).content == PNG
        assert [a["id"] for a in (await client.get("/v1/artifacts", headers=ALICE)).json()] == [
            upload["id"]
        ]
        # artifacts:admin sees the tenant's artifacts.
        assert (await client.get(f"{path}/metadata", headers=ADMIN)).status_code == 200
        assert len((await client.get("/v1/artifacts", headers=ADMIN)).json()) == 1
        # Another tenant's admin does not.
        other_admin = _headers("x", f"runs:read {ARTIFACTS_ADMIN_SCOPE}", tenant="other")
        assert (await client.get(path, headers=other_admin)).status_code == 404

        assert (await client.delete(path, headers=ALICE)).status_code == 204
        assert (await client.get(path, headers=ALICE)).status_code == 404


async def test_an_admin_may_delete_any_artifact_of_the_tenant() -> None:
    app = create_app(_settings())
    async with _client(app) as client:
        upload = await _upload(client, ALICE)
        response = await client.delete(f"/v1/artifacts/{upload['id']}", headers=ADMIN)
        assert response.status_code == 204


async def test_another_users_file_cannot_be_attached_to_a_run_or_a_message() -> None:
    app = create_app(_settings())
    async with _client(app) as client:
        upload = await _upload(client, ALICE)
        attach = [{"artifact_id": upload["id"]}]

        run = await client.post(
            "/v1/runs", headers=BOB, json={"agent": "reader", "input": "x", "attachments": attach}
        )
        missing = await client.post(
            "/v1/runs",
            headers=BOB,
            json={"agent": "reader", "input": "x", "attachments": [{"artifact_id": "nope"}]},
        )
        # The same refusal as a missing file: no oracle for other users' artifact ids.
        assert run.status_code == missing.status_code == 400
        assert run.json()["detail"] == f"attachment {upload['id']!r} was not found"

        conversation = await client.post("/v1/conversations", headers=BOB, json={"agent": "reader"})
        assert conversation.status_code == 201, conversation.text
        message = await client.post(
            f"/v1/conversations/{conversation.json()['id']}/messages",
            headers=BOB,
            json={"text": "what is this?", "attachments": attach},
        )
        assert message.status_code == 400
        assert "was not found" in message.json()["detail"]

        # Alice attaches her own file.
        mine = await client.post(
            "/v1/runs", headers=ALICE, json={"agent": "reader", "input": "x", "attachments": attach}
        )
        assert mine.status_code == 202, mine.text
        assert (await _finished(client, mine.json()["run_id"], ALICE))["status"] == "completed"


async def test_the_runtime_refuses_another_users_file_in_process() -> None:
    app = create_app(_settings())
    container = app.state.container
    artifact = Artifact(
        tenant_id="acme", user_id="alice", name="p.png", media_type="image/png", data=PNG
    )
    await container.artifacts.put(artifact)

    result = await container.runtime.run(
        RunRequest(
            agent="reader",
            input="x",
            tenant_id="acme",
            user_id="bob",
            attachments=(RunAttachment(artifact_id=artifact.id),),
        )
    )

    assert result.status == RunStatus.FAILED
    assert result.error == f"attachment {artifact.id!r} was not found"
    assert result.error_type == CapabilityError.__name__


async def test_the_user_of_a_run_reads_its_outputs_and_legacy_files() -> None:
    app = create_app(_settings())
    container = app.state.container
    async with _client(app) as client:
        started = await client.post(
            "/v1/runs", headers=ALICE, json={"agent": "reader", "input": "hello"}
        )
        run_id = started.json()["run_id"]
        await _finished(client, run_id, ALICE)
        # A run output, and a file stored before owners were recorded, under Alice's run.
        output = Artifact(
            tenant_id="acme",
            run_id=run_id,
            user_id="alice",
            name="o.txt",
            media_type="text/plain",
            data=b"out",
        )
        legacy = Artifact(
            tenant_id="acme", run_id=run_id, name="l.txt", media_type="text/plain", data=b"old"
        )
        orphan = Artifact(tenant_id="acme", name="x.txt", media_type="text/plain", data=b"x")
        for artifact in (output, legacy, orphan):
            await container.artifacts.put(artifact)

        assert (await client.get(f"/v1/artifacts/{legacy.id}", headers=ALICE)).content == b"old"
        assert (await client.get(f"/v1/artifacts/{legacy.id}", headers=BOB)).status_code == 404
        listed = await client.get("/v1/artifacts", params={"run_id": run_id}, headers=ALICE)
        assert {a["id"] for a in listed.json()} == {output.id, legacy.id}
        listed = await client.get("/v1/artifacts", params={"run_id": run_id}, headers=BOB)
        assert listed.json() == []
        # An ownerless file outside any run is for artifacts:admin only.
        assert (await client.get(f"/v1/artifacts/{orphan.id}", headers=ALICE)).status_code == 404
        assert (await client.get(f"/v1/artifacts/{orphan.id}", headers=ADMIN)).status_code == 200

        # Only the run's user files an upload under the run.
        refused = await client.post(
            "/v1/artifacts",
            params={"name": "p.png", "media_type": "image/png", "run_id": run_id},
            headers=BOB,
            content=PNG,
        )
        assert refused.status_code == 404
        filed = await _upload(client, ALICE, run_id=run_id)
        assert filed["run_id"] == run_id and filed["user_id"] == "alice"


# --- erasure and export --------------------------------------------------------------------


async def test_erasure_removes_an_upload_never_attached_and_keeps_other_users_files() -> None:
    app = create_app(_settings())
    container = app.state.container
    async with _client(app) as client:
        kept = await _upload(client, BOB)
        lonely = await _upload(client, ALICE)
    # A legacy run of Alice's that named Bob's file keeps Bob's file when Alice is erased.
    await container.runtime.runs.create(
        RunState(
            id="run-legacy",
            request=RunRequest(
                agent="reader",
                input="x",
                tenant_id="acme",
                user_id="alice",
                attachments=(RunAttachment(artifact_id=kept["id"]),),
            ),
            status=RunStatus.COMPLETED,
        )
    )
    service: UserDataService = container.user_data

    exported = await service.export_user("acme", "alice")
    assert [item["id"] for item in exported["artifacts"]] == [lonely["id"]]

    await service.erase_user("acme", "alice")

    assert await container.artifacts.describe(lonely["id"], "acme") is None
    assert await container.artifacts.describe(kept["id"], "acme") is not None


def test_migration_023_adds_a_nullable_owner_and_an_index() -> None:
    migration = (
        Path(__file__).parents[2]
        / "src/algen_agent_runtime/persistence/migrations/023_artifact_owner.sql"
    ).read_text(encoding="utf-8")
    sql = " ".join(
        line for line in migration.splitlines() if line.strip() and not line.startswith("--")
    )
    assert "ADD COLUMN IF NOT EXISTS user_id text;" in sql
    assert "NOT NULL" not in sql.replace("IS NOT NULL", "")
    assert "CREATE INDEX IF NOT EXISTS algen_agent_runtime_artifacts_user_idx" in sql
    for word in ("DROP", "DELETE", "UPDATE"):
        assert word not in sql.upper()
