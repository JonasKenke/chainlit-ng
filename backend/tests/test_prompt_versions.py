"""Prompt versioning, pinned sharing, updates, diff and sync contract tests."""

import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional, cast
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from chainlit.auth import get_current_user
from chainlit.config import config
from chainlit.data.base import BaseDataLayer
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.prompt_versioning import (
    MAX_PROMPT_CONTENT_BYTES,
    PromptContentTooLarge,
    PromptVersionLimitReached,
    build_diff,
)
from chainlit.server import app
from chainlit.types import PromptDict
from chainlit.user import PersistedUser

OWNER = PersistedUser(
    id="user-1", identifier="owner@example.com", createdAt="2024-01-01T00:00:00Z"
)
READER = PersistedUser(
    id="user-2", identifier="reader@example.com", createdAt="2024-01-01T00:00:00Z"
)
STRANGER = PersistedUser(
    id="user-3", identifier="stranger@example.com", createdAt="2024-01-01T00:00:00Z"
)


@pytest.fixture
async def data_layer(tmp_path: Path):
    layer = SQLAlchemyDataLayer(
        conninfo=f"sqlite+aiosqlite:///{tmp_path / 'versions.sqlite'}",
        storage_provider=None,
    )
    yield layer
    await layer.close()


@pytest.fixture
async def api(data_layer, monkeypatch):
    monkeypatch.setattr(config.features, "prompt_gallery", True)
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: data_layer)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@contextmanager
def acting_as(user: PersistedUser):
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def make_row(
    user_id: str,
    title: str = "Prompt",
    content: str = "body",
    shared: bool = False,
    prompt_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "id": prompt_id or str(uuid.uuid4()),
        "title": title,
        "content": content,
        "userId": user_id,
        "isShared": shared,
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-01T00:00:00Z",
    }


async def create_row(
    layer: SQLAlchemyDataLayer,
    user_id: str = OWNER.id,
    title: str = "Prompt",
    content: str = "body",
    shared: bool = False,
) -> PromptDict:
    created = await layer.create_prompt(make_row(user_id, title, content))  # type: ignore[arg-type]
    if shared:
        created = await layer.update_prompt(
            {**created, "isShared": True, "updatedAt": "2024-01-02T00:00:00Z"}
        )
    return created


async def update_row(
    layer: SQLAlchemyDataLayer, prompt: PromptDict, **changes
) -> PromptDict:
    payload: Dict[str, Any] = dict(prompt)
    payload.update(changes)
    payload["updatedAt"] = changes.get("updatedAt", "2024-02-01T00:00:00Z")
    return await layer.update_prompt(cast(PromptDict, payload))


async def add_copy(
    api: AsyncClient, source_id: str, body: Optional[Dict[str, Any]] = None
):
    return await api.post(
        f"/project/prompt/share/{source_id}/add",
        json=body if body is not None else None,
    )


# ---- SQLAlchemy migration, append and immutability ----


async def test_old_prompts_table_is_migrated_and_backfilled(
    tmp_path: Path, data_layer: SQLAlchemyDataLayer
):
    db_file = tmp_path / "legacy.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE prompts (
                    "id" TEXT PRIMARY KEY,
                    "userId" TEXT NOT NULL,
                    "title" TEXT NOT NULL,
                    "content" TEXT NOT NULL,
                    "isShared" BOOLEAN NOT NULL DEFAULT FALSE,
                    "createdAt" TEXT NOT NULL,
                    "updatedAt" TEXT NOT NULL
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                INSERT INTO prompts VALUES
                ('legacy-1', 'user-1', 'Legacy', 'old body', 1,
                 '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')
                """
            )
        )
    await engine.dispose()

    layer = SQLAlchemyDataLayer(
        conninfo=f"sqlite+aiosqlite:///{db_file}", storage_provider=None
    )
    try:
        prompt = await layer.get_prompt("legacy-1")
        assert prompt is not None
        assert prompt["content"] == "old body"
        assert prompt["version"] == 1
        assert prompt["contentHash"]

        versions = await layer.list_prompt_versions("legacy-1")
        assert len(versions) == 1
        assert versions[0]["version"] == 1
        assert versions[0]["createdBy"] == "user-1"
        version = await layer.get_prompt_version("legacy-1", versions[0]["id"])
        assert version is not None
        assert version["content"] == "old body"

        columns = await layer.execute_sql("PRAGMA table_info(prompts)", {})
        assert isinstance(columns, list)
        names = {row["name"] for row in columns}
        assert {"version", "contentHash", "sourcePromptId", "pinnedVersionId"} <= names
        # Prompt-only apps keep the pre-skills schema.
        assert "kind" not in names

        tables = await layer.execute_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'", {}
        )
        assert isinstance(tables, list)
        table_names = {row["name"] for row in tables}
        assert {"prompt_versions", "prompt_update_notices"} <= table_names
    finally:
        await layer.close()


async def test_create_and_update_append_immutable_contiguous_versions(
    data_layer: SQLAlchemyDataLayer,
):
    created = await create_row(data_layer, title="One")
    first = await data_layer.list_prompt_versions(created["id"])
    assert [item["version"] for item in first] == [1]
    first_id = first[0]["id"]

    updated = await update_row(data_layer, created, title="Two")
    assert updated["version"] == 2
    versions = await data_layer.list_prompt_versions(created["id"])
    assert [item["version"] for item in versions] == [2, 1]

    old = await data_layer.get_prompt_version(created["id"], first_id)
    assert old is not None
    assert old["title"] == "One"
    assert old["version"] == 1

    # Same fingerprint: no new version, even when updatedAt or isShared change.
    same = await update_row(data_layer, updated, isShared=True)
    assert same["version"] == 2
    assert len(await data_layer.list_prompt_versions(created["id"])) == 2

    # Title change is part of the fingerprint.
    third = await update_row(data_layer, same, title="Three")
    assert third["version"] == 3
    numbers = [
        item["version"] for item in await data_layer.list_prompt_versions(created["id"])
    ]
    assert numbers == [3, 2, 1]


async def test_version_cap_fails_without_partial_write(
    data_layer: SQLAlchemyDataLayer, monkeypatch
):
    monkeypatch.setattr("chainlit.data.sql_alchemy.MAX_PROMPT_VERSIONS", 2)
    created = await create_row(data_layer, title="v1")
    second = await update_row(data_layer, created, title="v2")

    with pytest.raises(PromptVersionLimitReached):
        await update_row(data_layer, second, title="v3")

    head = await data_layer.get_prompt(created["id"])
    assert head is not None
    assert head["title"] == "v2"
    assert head["version"] == 2
    assert len(await data_layer.list_prompt_versions(created["id"])) == 2


async def test_content_size_cap_rejects_without_writing(
    data_layer: SQLAlchemyDataLayer,
):
    too_large = "x" * (MAX_PROMPT_CONTENT_BYTES + 1)
    with pytest.raises(PromptContentTooLarge):
        await data_layer.create_prompt(make_row(OWNER.id, content=too_large))  # type: ignore[arg-type]
    assert await data_layer.list_prompts(OWNER.id) == []

    created = await create_row(data_layer, content="small")
    with pytest.raises(PromptContentTooLarge):
        await update_row(data_layer, created, content=too_large)
    head = await data_layer.get_prompt(created["id"])
    assert head is not None
    assert head["content"] == "small"
    assert head["version"] == 1
    assert len(await data_layer.list_prompt_versions(created["id"])) == 1


async def test_notices_are_clamped_and_raise_only(data_layer: SQLAlchemyDataLayer):
    source = await create_row(data_layer, shared=True)
    source_head = (await data_layer.list_prompt_versions(source["id"]))[0]
    payload: Dict[str, Any] = make_row(
        READER.id, title=source["title"], content=source["content"]
    )
    payload.update(
        {
            "sourcePromptId": source["id"],
            "pinnedVersionId": source_head["id"],
            "pinnedContentHash": source["contentHash"],
            "pinnedAt": "2024-01-02T00:00:00Z",
        }
    )
    copy = await data_layer.create_prompt(cast(PromptDict, payload))
    assert await data_layer.list_prompt_updates(READER.id) == []

    await update_row(data_layer, source, content="source v2")
    updates = await data_layer.list_prompt_updates(READER.id)
    assert len(updates) == 1
    assert updates[0]["latestVersion"] == 2
    assert updates[0]["notifiedVersion"] == 0

    assert await data_layer.ack_prompt_update(READER.id, copy["id"], 99) == 2
    assert await data_layer.list_prompt_updates(READER.id) == []
    # A lower acknowledgment must not move the notice backwards.
    assert await data_layer.ack_prompt_update(READER.id, copy["id"], 1) == 2

    await update_row(data_layer, source, content="source v3")
    updates = await data_layer.list_prompt_updates(READER.id)
    assert [item["latestVersion"] for item in updates] == [3]


# ---- API: sharing, versions and sanitization ----


async def test_shared_prompt_metadata_is_public_and_owner_safe(api, data_layer):
    source = await create_row(data_layer, title="Shared", shared=True)
    versions = await data_layer.list_prompt_versions(source["id"])

    response = await api.get(f"/project/prompt/share/{source['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1
    assert body["currentVersionId"] == versions[0]["id"]
    assert body["currentContentHash"] == source["contentHash"]
    assert "userId" not in body
    assert "createdBy" not in body

    updated = await update_row(data_layer, source, content="source v2")
    after = await api.get(f"/project/prompt/share/{source['id']}")
    assert after.status_code == 200
    assert after.json()["version"] == 2
    head = (await data_layer.list_prompt_versions(source["id"]))[0]
    assert after.json()["currentVersionId"] == head["id"]
    assert after.json()["currentContentHash"] == updated["contentHash"]


async def test_public_version_endpoints_work_anonymously(api, data_layer):
    source = await create_row(data_layer, title="Shared", content="v1", shared=True)
    await update_row(data_layer, source, content="v2")
    head = (await data_layer.list_prompt_versions(source["id"]))[0]

    listing = await api.get(f"/project/prompt/share/{source['id']}/versions")
    snapshot = await api.get(
        f"/project/prompt/share/{source['id']}/versions/{head['id']}"
    )

    assert listing.status_code == 200
    assert [item["version"] for item in listing.json()] == [2, 1]
    assert all("content" not in item for item in listing.json())
    assert all("userId" not in item for item in listing.json())
    assert all("createdBy" not in item for item in listing.json())

    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["id"] == head["id"]
    assert body["version"] == 2
    assert body["content"] == "v2"
    assert body["contentHash"] == head["contentHash"]
    assert "userId" not in body
    assert "createdBy" not in body


async def test_public_version_endpoints_404_for_unshared_or_deleted(api, data_layer):
    unshared = await create_row(data_layer, shared=False)
    unshared_head = (await data_layer.list_prompt_versions(unshared["id"]))[0]
    assert (
        await api.get(f"/project/prompt/share/{unshared['id']}/versions")
    ).status_code == 404
    assert (
        await api.get(
            f"/project/prompt/share/{unshared['id']}/versions/{unshared_head['id']}"
        )
    ).status_code == 404

    deleted = await create_row(data_layer, shared=True)
    deleted_head = (await data_layer.list_prompt_versions(deleted["id"]))[0]
    assert await data_layer.delete_prompt(deleted["id"], OWNER.id) is True
    assert (
        await api.get(f"/project/prompt/share/{deleted['id']}/versions")
    ).status_code == 404
    assert (
        await api.get(
            f"/project/prompt/share/{deleted['id']}/versions/{deleted_head['id']}"
        )
    ).status_code == 404


async def test_public_version_endpoint_rejects_foreign_or_unknown_version(
    api, data_layer
):
    source = await create_row(data_layer, shared=True)
    other = await create_row(data_layer, shared=True)
    foreign = (await data_layer.list_prompt_versions(other["id"]))[0]

    assert (
        await api.get(f"/project/prompt/share/{source['id']}/versions/{foreign['id']}")
    ).status_code == 404
    assert (
        await api.get(f"/project/prompt/share/{source['id']}/versions/unknown")
    ).status_code == 404


async def test_versions_are_listed_for_owned_and_shared_prompts(api, data_layer):
    source = await create_row(data_layer, title="Shared", shared=True)

    with acting_as(OWNER):
        owned = await api.get(f"/project/prompts/{source['id']}/versions")
    assert owned.status_code == 200
    assert owned.json()[0]["createdBy"] == OWNER.id

    with acting_as(READER):
        shared = await api.get(f"/project/prompts/{source['id']}/versions")
        version_id = shared.json()[0]["id"]
        one = await api.get(f"/project/prompts/{source['id']}/versions/{version_id}")
    assert shared.status_code == 200
    assert "createdBy" not in shared.json()[0]
    assert "content" not in shared.json()[0]
    assert one.status_code == 200
    assert one.json()["content"] == "body"
    assert "createdBy" not in one.json()


async def test_versions_hidden_from_non_owner_when_unshared(api, data_layer):
    source = await create_row(data_layer, shared=False)
    version_id = (await data_layer.list_prompt_versions(source["id"]))[0]["id"]
    with acting_as(READER):
        listing = await api.get(f"/project/prompts/{source['id']}/versions")
        single = await api.get(f"/project/prompts/{source['id']}/versions/{version_id}")
    assert listing.status_code == 404
    assert single.status_code == 404


async def test_add_shared_prompt_pins_head_and_is_idempotent(api, data_layer):
    source = await create_row(data_layer, title="Source", content="v1", shared=True)
    head = (await data_layer.list_prompt_versions(source["id"]))[0]

    with acting_as(READER):
        response = await add_copy(api, source["id"])
        duplicate = await add_copy(api, source["id"])

    assert response.status_code == 200
    copy = response.json()
    assert copy["sourcePromptId"] == source["id"]
    assert copy["pinnedVersionId"] == head["id"]
    assert copy["pinnedContentHash"] == head["contentHash"]
    assert copy["pinnedAt"]
    assert copy["isShared"] is False
    assert copy["version"] == 1

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "already_added"


async def test_add_shared_prompt_can_pin_an_older_version(api, data_layer):
    source = await create_row(data_layer, title="Source", content="v1", shared=True)
    v1 = (await data_layer.list_prompt_versions(source["id"]))[0]
    await update_row(data_layer, source, content="v2")

    with acting_as(READER):
        response = await add_copy(api, source["id"], {"versionId": v1["id"]})

    assert response.status_code == 200
    copy = response.json()
    assert copy["content"] == "v1"
    assert copy["pinnedVersionId"] == v1["id"]
    assert copy["pinnedContentHash"] == v1["contentHash"]


async def test_add_shared_prompt_rejects_unknown_version(api, data_layer):
    source = await create_row(data_layer, shared=True)
    with acting_as(READER):
        response = await add_copy(api, source["id"], {"versionId": "missing"})
    assert response.status_code == 404


# ---- API: updates and acknowledgment ----


async def test_updates_are_one_shot_and_ack_never_lowers(api, data_layer):
    source = await create_row(data_layer, title="Source", content="v1", shared=True)
    with acting_as(READER):
        assert (await add_copy(api, source["id"])).status_code == 200

    await update_row(data_layer, source, content="v2")

    with acting_as(READER):
        first = await api.get("/project/prompts/updates")
        assert first.status_code == 200
        assert [item["latestVersion"] for item in first.json()] == [2]
        copy_id = first.json()[0]["promptId"]

        acked = await api.post(
            "/project/prompts/updates/ack",
            json={"promptId": copy_id, "version": 2},
        )
        assert acked.status_code == 200
        assert acked.json()["notifiedVersion"] == 2
        assert (await api.get("/project/prompts/updates")).json() == []

        lowered = await api.post(
            "/project/prompts/updates/ack",
            json={"promptId": copy_id, "version": 1},
        )
        assert lowered.json()["notifiedVersion"] == 2

    await update_row(data_layer, source, content="v3")
    with acting_as(READER):
        second = await api.get("/project/prompts/updates")
    assert [item["latestVersion"] for item in second.json()] == [3]


async def test_ack_requires_ownership(api, data_layer):
    source = await create_row(data_layer, shared=True)
    with acting_as(READER):
        copy = (await add_copy(api, source["id"])).json()

    with acting_as(STRANGER):
        response = await api.post(
            "/project/prompts/updates/ack",
            json={"promptId": copy["id"], "version": 1},
        )
    assert response.status_code == 404


# ---- API: diff ----


async def test_diff_returns_structured_rows_for_pinned_to_local(api, data_layer):
    source = await create_row(
        data_layer, title="Source", content="line one\n", shared=True
    )
    with acting_as(READER):
        copy = (await add_copy(api, source["id"])).json()
        edited = await api.put(
            f"/project/prompts/{copy['id']}",
            json={"content": "line one\nline two\n"},
        )
        assert edited.status_code == 200
        local_version = edited.json()["version"]
        versions = (await api.get(f"/project/prompts/{copy['id']}/versions")).json()
        target = next(v for v in versions if v["version"] == local_version)
        response = await api.get(
            f"/project/prompts/{copy['id']}/diff",
            params={"from": "pinned", "to": target["id"]},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["from"] == "pinned"
    assert body["to"] == target["id"]
    assert body["fromVersion"] == 1
    assert body["toVersion"] == local_version
    assert body["fromVersionId"] != body["toVersionId"]
    assert body["maxRows"] > 0
    assert body["maxBytes"] > 0
    assert body["truncated"] is False
    assert any(row["op"] in {"insert", "replace"} for row in body["rows"])
    assert all("<" not in (row["oldText"] or "") for row in body["rows"])


async def test_diff_rejects_prompt_without_pin(api, data_layer):
    prompt = await create_row(data_layer, shared=True)
    version_id = (await data_layer.list_prompt_versions(prompt["id"]))[0]["id"]
    with acting_as(READER):
        response = await api.get(
            f"/project/prompts/{prompt['id']}/diff",
            params={"from": "pinned", "to": version_id},
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "not_pinned"


def test_diff_rows_are_capped():
    old = "\n".join(f"old {index}" for index in range(50))
    new = "\n".join(f"new {index}" for index in range(50))
    rows, truncated = build_diff(old, new, max_rows=10, max_bytes=1_000_000)
    assert len(rows) == 10
    assert truncated is True

    rows, truncated = build_diff("a" * 100, "b" * 100, max_rows=10, max_bytes=50)
    assert rows == []
    assert truncated is True


# ---- API: sync and detach ----


async def test_sync_happy_path_appends_version(api, data_layer):
    source = await create_row(data_layer, title="Source", content="v1", shared=True)
    with acting_as(READER):
        copy = (await add_copy(api, source["id"])).json()

    await update_row(data_layer, source, content="v2")
    target = (await data_layer.list_prompt_versions(source["id"]))[0]

    with acting_as(READER):
        response = await api.post(
            f"/project/prompts/{copy['id']}/sync", json={"versionId": target["id"]}
        )
        versions = await api.get(f"/project/prompts/{copy['id']}/versions")

    assert response.status_code == 200
    synced = response.json()
    assert synced["content"] == "v2"
    assert synced["version"] == 2
    assert synced["pinnedVersionId"] == target["id"]
    assert synced["pinnedContentHash"] == target["contentHash"]
    assert [item["version"] for item in versions.json()] == [2, 1]


async def test_sync_requires_confirmation_for_local_changes(api, data_layer):
    source = await create_row(data_layer, title="Source", content="v1", shared=True)
    with acting_as(READER):
        copy = (await add_copy(api, source["id"])).json()
        await api.put(
            f"/project/prompts/{copy['id']}",
            json={"content": "local edit"},
        )

    await update_row(data_layer, source, content="v2")
    target = (await data_layer.list_prompt_versions(source["id"]))[0]

    with acting_as(READER):
        blocked = await api.post(
            f"/project/prompts/{copy['id']}/sync", json={"versionId": target["id"]}
        )
        still_local = await api.get(f"/project/prompts/{copy['id']}/versions")
        confirmed = await api.post(
            f"/project/prompts/{copy['id']}/sync",
            json={"versionId": target["id"], "confirm": True},
        )

    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "local_changes"
    assert len(still_local.json()) == 2
    assert confirmed.status_code == 200
    assert confirmed.json()["content"] == "v2"
    assert confirmed.json()["version"] == 3


async def test_sync_rejects_stale_preview(api, data_layer):
    source = await create_row(data_layer, shared=True)
    with acting_as(READER):
        copy = (await add_copy(api, source["id"])).json()
    await update_row(data_layer, source, content="v2")
    target = (await data_layer.list_prompt_versions(source["id"]))[0]

    with acting_as(READER):
        response = await api.post(
            f"/project/prompts/{copy['id']}/sync",
            json={"versionId": target["id"], "expectedLocalHash": "stale"},
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "preview_stale"


async def test_sync_rejects_already_current_and_foreign_versions(api, data_layer):
    source = await create_row(data_layer, shared=True)
    head = (await data_layer.list_prompt_versions(source["id"]))[0]
    other = await create_row(data_layer, shared=True)
    foreign = (await data_layer.list_prompt_versions(other["id"]))[0]

    with acting_as(READER):
        copy = (await add_copy(api, source["id"])).json()
        current = await api.post(
            f"/project/prompts/{copy['id']}/sync", json={"versionId": head["id"]}
        )
        conflict = await api.post(
            f"/project/prompts/{copy['id']}/sync", json={"versionId": foreign["id"]}
        )

    assert current.status_code == 409
    assert current.json()["detail"]["code"] == "already_current"
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "version_conflict"


async def test_sync_rejects_unshared_source(api, data_layer):
    source = await create_row(data_layer, content="v1", shared=True)
    with acting_as(READER):
        copy = (await add_copy(api, source["id"])).json()
    head = (await data_layer.list_prompt_versions(source["id"]))[0]
    await update_row(data_layer, source, isShared=False, content="v2")

    with acting_as(READER):
        response = await api.post(
            f"/project/prompts/{copy['id']}/sync", json={"versionId": head["id"]}
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_unavailable"


async def test_sync_is_owner_only(api, data_layer):
    source = await create_row(data_layer, shared=True)
    with acting_as(READER):
        copy = (await add_copy(api, source["id"])).json()
    await update_row(data_layer, source, content="v2")
    target = (await data_layer.list_prompt_versions(source["id"]))[0]

    with acting_as(STRANGER):
        response = await api.post(
            f"/project/prompts/{copy['id']}/sync", json={"versionId": target["id"]}
        )
    assert response.status_code == 404


async def test_detach_clears_pin_and_keeps_history(api, data_layer):
    source = await create_row(data_layer, content="v1", shared=True)
    with acting_as(READER):
        copy = (await add_copy(api, source["id"])).json()
        response = await api.post(f"/project/prompts/{copy['id']}/detach")
        again = await api.post(f"/project/prompts/{copy['id']}/detach")
        versions = await api.get(f"/project/prompts/{copy['id']}/versions")

    assert response.status_code == 200
    detached = response.json()
    assert detached["sourcePromptId"] is None
    assert detached["pinnedVersionId"] is None
    assert detached["pinnedContentHash"] is None
    assert detached["pinnedAt"] is None
    assert detached["content"] == "v1"
    assert len(versions.json()) == 1
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "not_pinned"


# ---- API: caps and capability fallbacks ----


async def test_create_route_rejects_oversized_content(api):
    with acting_as(OWNER):
        response = await api.post(
            "/project/prompts",
            json={"title": "Too big", "content": "x" * (MAX_PROMPT_CONTENT_BYTES + 1)},
        )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "content_too_large"


async def test_version_routes_return_501_for_unsupported_layer(api, monkeypatch):
    unsupported = AsyncMock(spec=BaseDataLayer)
    unsupported.get_prompt.return_value = make_row(OWNER.id, shared=True)
    unsupported.list_prompt_versions.side_effect = NotImplementedError(
        "Prompt versioning is not implemented for this data layer"
    )
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: unsupported)

    with acting_as(OWNER):
        response = await api.get("/project/prompts/anything/versions")
    assert response.status_code == 501


async def test_prompt_versioning_capability_is_exposed(api, data_layer):
    response = await api.get("/project/settings")
    assert response.status_code == 200
    body = response.json()
    assert body["promptGallery"] is True
    assert body["promptVersioning"] is True

    from chainlit.server import _supports_prompt_versioning

    assert _supports_prompt_versioning(data_layer) is True
