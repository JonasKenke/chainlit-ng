"""Skill versioning, sharing, pinned updates and review-gate contract tests."""

import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from chainlit.auth import get_current_user
from chainlit.config import config
from chainlit.data.base import BaseDataLayer
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.prompt_versioning import (
    MAX_PROMPT_CONTENT_BYTES,
    PromptVersionLimitReached,
)
from chainlit.server import app, skills_router
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
        conninfo=f"sqlite+aiosqlite:///{tmp_path / 'skills-sharing.sqlite'}",
        storage_provider=None,
    )
    yield layer
    await layer.close()


_ACTIVE_APP: Optional[FastAPI] = None


@pytest.fixture
async def api(data_layer, monkeypatch):
    global _ACTIVE_APP
    monkeypatch.setattr(config.features, "skills", True)
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: data_layer)
    test_app = FastAPI()
    test_app.include_router(skills_router)
    _ACTIVE_APP = test_app
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    _ACTIVE_APP = None


_UNSET = object()


@contextmanager
def acting_as(user: PersistedUser):
    target = _ACTIVE_APP or app
    previous = target.dependency_overrides.get(get_current_user, _UNSET)
    target.dependency_overrides[get_current_user] = lambda: user
    try:
        yield
    finally:
        if previous is _UNSET:
            target.dependency_overrides.pop(get_current_user, None)
        else:
            target.dependency_overrides[get_current_user] = previous  # type: ignore[assignment]


def make_skill(
    user_id: str,
    title: str = "alpha",
    content: str = "Alpha body.",
    description: str = "Alpha description",
    enabled: bool = False,
    skill_id: Optional[str] = None,
    imported_from: Optional[str] = None,
    content_hash: Optional[str] = None,
) -> PromptDict:
    return PromptDict(
        id=skill_id or str(uuid.uuid4()),
        userId=user_id,
        title=title,
        content=content,
        isShared=False,
        createdAt="2024-01-01T00:00:00Z",
        updatedAt="2024-01-01T00:00:00Z",
        kind="skill",
        description=description,
        enabled=enabled,
        importedFrom=imported_from,
        importedAt="2024-01-01T00:00:00Z" if imported_from else None,
        contentHash=content_hash,
    )


async def create_skill(
    layer: SQLAlchemyDataLayer,
    user_id: str = OWNER.id,
    title: str = "alpha",
    content: str = "Alpha body.",
    description: str = "Alpha description",
    **kwargs: Any,
) -> PromptDict:
    return await layer.create_skill(
        make_skill(
            user_id,
            title=title,
            content=content,
            description=description,
            **kwargs,
        )
    )


async def update_row(
    layer: SQLAlchemyDataLayer, skill: PromptDict, **changes: Any
) -> PromptDict:
    stored = await layer.get_skill(skill["id"], skill["userId"])
    payload: Dict[str, Any] = dict(stored or skill)
    payload.update(changes)
    payload["updatedAt"] = changes.get("updatedAt", "2024-02-01T00:00:00Z")
    return await layer.update_skill(cast(PromptDict, payload))


async def share_skill(api: AsyncClient, skill_id: str, shared: bool = True, user=OWNER):
    with acting_as(user):
        return await api.post(
            f"/project/skills/{skill_id}/share", json={"isShared": shared}
        )


async def add_copy(
    api: AsyncClient,
    source_id: str,
    body: Optional[dict] = None,
    user=READER,
):
    with acting_as(user):
        return await api.post(
            f"/project/skill/share/{source_id}/add",
            json=body if body is not None else None,
        )


async def owner_versions(api: AsyncClient, skill_id: str, user=OWNER) -> list[dict]:
    with acting_as(user):
        response = await api.get(f"/project/skills/{skill_id}/versions")
    assert response.status_code == 200
    return response.json()


# ---- versions, description fingerprints and no-op edits ----


async def test_create_and_update_append_skill_versions(
    data_layer: SQLAlchemyDataLayer,
):
    skill = await create_skill(data_layer, description="first description")
    versions = await data_layer.list_prompt_versions(skill["id"])
    assert [version["version"] for version in versions] == [1]
    assert versions[0]["kind"] == "skill"
    assert versions[0]["description"] == "first description"
    assert versions[0]["contentHash"].startswith("sha256:")

    renamed = await update_row(data_layer, skill, title="beta")
    assert renamed["version"] == 2

    # Sharing and enabling are head-only changes.
    shared = await update_row(data_layer, renamed, isShared=True)
    assert shared["version"] == 2
    enabled = await update_row(data_layer, shared, enabled=True)
    assert enabled["version"] == 2

    # Description is part of a skill fingerprint.
    described = await update_row(data_layer, enabled, description="second")
    assert described["version"] == 3
    assert [
        version["version"]
        for version in await data_layer.list_prompt_versions(skill["id"])
    ] == [3, 2, 1]

    # A no-op edit keeps the head version.
    same = await update_row(data_layer, described, updatedAt="2024-03-01T00:00:00Z")
    assert same["version"] == 3


async def test_skill_versions_are_bounded(data_layer: SQLAlchemyDataLayer, monkeypatch):
    monkeypatch.setattr("chainlit.data.sql_alchemy.MAX_PROMPT_VERSIONS", 2)
    skill = await create_skill(data_layer)
    second = await update_row(data_layer, skill, content="v2")

    with pytest.raises(PromptVersionLimitReached):
        await update_row(data_layer, second, content="v3")

    head = await data_layer.get_skill(skill["id"], OWNER.id)
    assert head is not None
    assert head["version"] == 2
    assert head["content"] == "v2"
    assert len(await data_layer.list_prompt_versions(skill["id"])) == 2


async def test_skill_content_cap_rejects_without_writing(api):
    with acting_as(OWNER):
        response = await api.post(
            "/project/skills",
            json={
                "name": "oversize",
                "description": "Too big",
                "content": "x" * (MAX_PROMPT_CONTENT_BYTES + 1),
            },
        )
        listed = await api.get("/project/skills")
    assert response.status_code == 400
    assert listed.json() == []


# ---- sharing and public projections ----


async def test_share_is_owner_only_and_unshare_hides_public_reads(
    api: AsyncClient, data_layer: SQLAlchemyDataLayer
):
    skill = await create_skill(data_layer)
    response = await share_skill(api, skill["id"], True)
    assert response.status_code == 200
    body = response.json()
    assert body["shareUrl"] == f"/skill/{skill['id']}"
    assert body["skill"]["isShared"] is True
    assert body["skill"]["sharedAt"]

    public = await api.get(f"/project/skill/share/{skill['id']}")
    assert public.status_code == 200

    stranger = await share_skill(api, skill["id"], False, user=STRANGER)
    assert stranger.status_code == 404

    unshared = await share_skill(api, skill["id"], False)
    assert unshared.status_code == 200
    assert (await api.get(f"/project/skill/share/{skill['id']}")).status_code == 404
    assert (
        await api.get(f"/project/skill/share/{skill['id']}/versions")
    ).status_code == 404


async def test_public_skill_payload_is_sanitized(
    api: AsyncClient, data_layer: SQLAlchemyDataLayer
):
    skill = await create_skill(data_layer, imported_from="https://example.com/alpha.md")
    await share_skill(api, skill["id"], True)
    updated = await update_row(data_layer, skill, content="Alpha body v2")
    head = (await data_layer.list_prompt_versions(skill["id"]))[0]

    response = await api.get(f"/project/skill/share/{skill['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "alpha"
    assert body["description"] == "Alpha description"
    assert body["content"] == "Alpha body v2"
    assert body["version"] == 2
    assert body["currentVersionId"] == head["id"]
    assert body["contentHash"] == head["contentHash"]
    assert body["updatedAt"] == updated["updatedAt"]
    assert body["sharedAt"]
    assert body["authorLabel"] == "Chainlit user"
    for forbidden in ("userId", "createdBy", "enabled", "importedFrom"):
        assert forbidden not in body

    listing = await api.get(f"/project/skill/share/{skill['id']}/versions")
    assert listing.status_code == 200
    assert [item["version"] for item in listing.json()] == [2, 1]
    assert all("content" not in item for item in listing.json())
    assert all("userId" not in item for item in listing.json())
    assert all("createdBy" not in item for item in listing.json())

    snapshot = await api.get(
        f"/project/skill/share/{skill['id']}/versions/{head['id']}"
    )
    assert snapshot.status_code == 200
    assert snapshot.json()["content"] == "Alpha body v2"
    assert snapshot.json()["description"] == "Alpha description"
    assert "createdBy" not in snapshot.json()


async def test_public_skill_routes_404_for_unknown_unshared_and_foreign_versions(
    api: AsyncClient, data_layer: SQLAlchemyDataLayer
):
    unshared = await create_skill(data_layer)
    versions = await data_layer.list_prompt_versions(unshared["id"])

    assert (await api.get("/project/skill/share/unknown")).status_code == 404
    assert (await api.get(f"/project/skill/share/{unshared['id']}")).status_code == 404
    assert (
        await api.get(f"/project/skill/share/{unshared['id']}/versions")
    ).status_code == 404
    assert (
        await api.get(
            f"/project/skill/share/{unshared['id']}/versions/{versions[0]['id']}"
        )
    ).status_code == 404

    shared = await create_skill(data_layer, title="shared")
    await share_skill(api, shared["id"], True)
    other = await create_skill(data_layer, title="other")
    foreign = (await data_layer.list_prompt_versions(other["id"]))[0]

    assert (
        await api.get(f"/project/skill/share/{shared['id']}/versions/{foreign['id']}")
    ).status_code == 404
    assert (
        await api.get(f"/project/skill/share/{shared['id']}/versions/unknown")
    ).status_code == 404


# ---- adding a shared skill ----


async def test_add_shared_skill_creates_a_disabled_pinned_copy(
    api: AsyncClient, data_layer: SQLAlchemyDataLayer
):
    source = await create_skill(data_layer)
    await share_skill(api, source["id"], True)
    head = (await data_layer.list_prompt_versions(source["id"]))[0]

    response = await add_copy(api, source["id"])
    assert response.status_code == 200
    copy = response.json()
    assert copy["userId"] == READER.id
    assert copy["enabled"] is False
    assert copy["isShared"] is False
    assert copy["version"] == 1
    assert copy["sourcePromptId"] == source["id"]
    assert copy["pinnedVersionId"] == head["id"]
    assert copy["pinnedContentHash"] == head["contentHash"]
    assert copy["updateSeenVersionId"] == head["id"]
    assert copy["pinnedAt"]
    assert copy["content"] == "Alpha body."

    duplicate = await add_copy(api, source["id"])
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "already_added"


async def test_add_shared_skill_can_pin_an_older_version_and_rejects_name_conflict(
    api: AsyncClient, data_layer: SQLAlchemyDataLayer
):
    source = await create_skill(data_layer, content="v1")
    v1 = (await data_layer.list_prompt_versions(source["id"]))[0]
    await update_row(data_layer, source, content="v2")
    await share_skill(api, source["id"], True)

    older = await add_copy(api, source["id"], {"versionId": v1["id"]})
    assert older.status_code == 200
    assert older.json()["content"] == "v1"
    assert older.json()["pinnedVersionId"] == v1["id"]

    await create_skill(data_layer, user_id=STRANGER.id, title="alpha")
    conflict = await add_copy(api, source["id"], user=STRANGER)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "name_taken"

    unknown = await add_copy(api, source["id"], {"versionId": "missing"}, user=STRANGER)
    assert unknown.status_code == 404


# ---- updates and seen notices ----


async def test_skill_updates_notice_fires_once_and_seen_never_lowers(
    api: AsyncClient, data_layer: SQLAlchemyDataLayer
):
    source = await create_skill(data_layer, content="v1")
    await share_skill(api, source["id"], True)
    copy = (await add_copy(api, source["id"])).json()

    with acting_as(READER):
        assert (await api.get("/project/skills/updates")).json() == []

    await update_row(data_layer, source, content="v2")
    with acting_as(READER):
        first = await api.get("/project/skills/updates")
        assert first.status_code == 200
        assert [item["latestVersion"] for item in first.json()] == [2]
        assert first.json()[0]["skillId"] == copy["id"]

        acked = await api.post(
            f"/project/skills/{copy['id']}/updates/seen", json={"version": 99}
        )
        assert acked.status_code == 200
        assert acked.json()["version"] == 2
        seen_id = acked.json()["updateSeenVersionId"]
        assert seen_id == first.json()[0]["latestVersionId"]
        assert (await api.get("/project/skills/updates")).json() == []

        lowered = await api.post(
            f"/project/skills/{copy['id']}/updates/seen", json={"version": 1}
        )
        assert lowered.json()["version"] == 2
        assert lowered.json()["updateSeenVersionId"] == seen_id

    await update_row(data_layer, source, content="v3")
    with acting_as(READER):
        second = await api.get("/project/skills/updates")
    assert [item["latestVersion"] for item in second.json()] == [3]


# ---- update from source ----


async def test_update_from_source_forces_review_and_marks_seen(
    api: AsyncClient, data_layer: SQLAlchemyDataLayer
):
    source = await create_skill(data_layer, content="v1")
    await share_skill(api, source["id"], True)
    copy = (await add_copy(api, source["id"])).json()

    copy_head = (await owner_versions(api, copy["id"], user=READER))[0]
    with acting_as(READER):
        enabled = await api.put(
            f"/project/skills/{copy['id']}",
            json={
                "enabled": True,
                "reviewedContentHash": copy_head["contentHash"],
            },
        )
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True

    await update_row(data_layer, source, content="v2")
    target = (await data_layer.list_prompt_versions(source["id"]))[0]

    with acting_as(READER):
        response = await api.post(
            f"/project/skills/{copy['id']}/update-from-source",
            json={"versionId": target["id"]},
        )
        versions = await owner_versions(api, copy["id"], user=READER)
        updates = await api.get("/project/skills/updates")
        stored = await api.get(f"/project/skills/{copy['id']}")

    assert response.status_code == 200
    synced = response.json()
    assert synced["content"] == "v2"
    assert synced["enabled"] is False
    assert synced["version"] == 2
    assert synced["pinnedVersionId"] == target["id"]
    assert synced["pinnedContentHash"] == target["contentHash"]
    assert synced["updateSeenVersionId"] == target["id"]
    assert [version["version"] for version in versions] == [2, 1]
    assert updates.json() == []
    assert stored.json()["enabled"] is False


async def test_update_from_source_requires_confirm_and_rejects_a_stale_preview(
    api: AsyncClient, data_layer: SQLAlchemyDataLayer
):
    source = await create_skill(data_layer, content="v1")
    await share_skill(api, source["id"], True)
    copy = (await add_copy(api, source["id"])).json()

    with acting_as(READER):
        edited = await api.put(
            f"/project/skills/{copy['id']}", json={"content": "local edit"}
        )
    assert edited.status_code == 200
    assert edited.json()["enabled"] is False
    assert edited.json()["version"] == 2
    local_hash = (await owner_versions(api, copy["id"], user=READER))[0]["contentHash"]

    await update_row(data_layer, source, content="v2")
    target = (await data_layer.list_prompt_versions(source["id"]))[0]

    with acting_as(READER):
        stale = await api.post(
            f"/project/skills/{copy['id']}/update-from-source",
            json={"versionId": target["id"], "expectedLocalHash": "stale"},
        )
        blocked = await api.post(
            f"/project/skills/{copy['id']}/update-from-source",
            json={
                "versionId": target["id"],
                "expectedLocalHash": local_hash,
            },
        )
        unchanged = await api.get(f"/project/skills/{copy['id']}")
        confirmed = await api.post(
            f"/project/skills/{copy['id']}/update-from-source",
            json={
                "versionId": target["id"],
                "expectedLocalHash": local_hash,
                "confirm": True,
            },
        )

    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "preview_stale"
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "local_changes"
    assert unchanged.json()["content"] == "local edit"
    assert unchanged.json()["version"] == 2
    assert confirmed.status_code == 200
    assert confirmed.json()["content"] == "v2"
    assert confirmed.json()["version"] == 3
    assert confirmed.json()["enabled"] is False


async def test_update_from_source_conflict_codes(
    api: AsyncClient, data_layer: SQLAlchemyDataLayer
):
    local = await create_skill(data_layer, title="local")
    with acting_as(READER):
        not_pinned = await api.post(
            f"/project/skills/{local['id']}/update-from-source", json={}
        )
    assert not_pinned.status_code == 404

    with acting_as(OWNER):
        not_pinned = await api.post(
            f"/project/skills/{local['id']}/update-from-source", json={}
        )
    assert not_pinned.status_code == 409
    assert not_pinned.json()["detail"]["code"] == "not_pinned"

    source = await create_skill(data_layer, title="source", content="v1")
    await share_skill(api, source["id"], True)
    copy = (await add_copy(api, source["id"])).json()
    other = await create_skill(data_layer, title="other", content="other")
    foreign = (await data_layer.list_prompt_versions(other["id"]))[0]

    with acting_as(READER):
        conflict = await api.post(
            f"/project/skills/{copy['id']}/update-from-source",
            json={"versionId": foreign["id"]},
        )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "version_conflict"

    await share_skill(api, source["id"], False)
    with acting_as(READER):
        unavailable = await api.post(
            f"/project/skills/{copy['id']}/update-from-source", json={}
        )
    assert unavailable.status_code == 409
    assert unavailable.json()["detail"]["code"] == "source_unavailable"


async def test_detach_clears_the_pin_and_keeps_history(
    api: AsyncClient, data_layer: SQLAlchemyDataLayer
):
    source = await create_skill(data_layer, content="v1")
    await share_skill(api, source["id"], True)
    copy = (await add_copy(api, source["id"])).json()

    with acting_as(READER):
        detached = await api.post(f"/project/skills/{copy['id']}/detach")
        again = await api.post(f"/project/skills/{copy['id']}/detach")
        versions = await owner_versions(api, copy["id"], user=READER)
        update = await api.post(
            f"/project/skills/{copy['id']}/update-from-source", json={}
        )

    assert detached.status_code == 200
    body = detached.json()
    assert body["sourcePromptId"] is None
    assert body["pinnedVersionId"] is None
    assert body["pinnedContentHash"] is None
    assert body["pinnedAt"] is None
    assert body["updateSeenVersionId"] is None
    assert body["content"] == "v1"
    assert body["version"] == 1
    assert [version["version"] for version in versions] == [1]
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "not_pinned"
    assert update.status_code == 409
    assert update.json()["detail"]["code"] == "not_pinned"


async def test_detach_is_owner_only(api: AsyncClient, data_layer: SQLAlchemyDataLayer):
    source = await create_skill(data_layer)
    await share_skill(api, source["id"], True)
    copy = (await add_copy(api, source["id"])).json()

    with acting_as(STRANGER):
        response = await api.post(f"/project/skills/{copy['id']}/detach")
    assert response.status_code == 404


async def test_update_from_source_is_owner_only(
    api: AsyncClient, data_layer: SQLAlchemyDataLayer
):
    source = await create_skill(data_layer)
    await share_skill(api, source["id"], True)
    copy = (await add_copy(api, source["id"])).json()

    with acting_as(STRANGER):
        response = await api.post(
            f"/project/skills/{copy['id']}/update-from-source", json={}
        )
    assert response.status_code == 404


# ---- review gate ----


async def test_review_gate_requires_the_current_version_hash(
    api: AsyncClient, data_layer: SQLAlchemyDataLayer
):
    imported = await create_skill(
        data_layer, title="imported", imported_from="https://example.com/s.md"
    )
    head = (await owner_versions(api, imported["id"]))[0]

    with acting_as(OWNER):
        missing = await api.put(
            f"/project/skills/{imported['id']}", json={"enabled": True}
        )
        wrong = await api.put(
            f"/project/skills/{imported['id']}",
            json={"enabled": True, "reviewedContentHash": "stale"},
        )
        accepted = await api.put(
            f"/project/skills/{imported['id']}",
            json={
                "enabled": True,
                "reviewedContentHash": head["contentHash"],
            },
        )
    assert missing.status_code == 409
    assert missing.json()["detail"]["code"] == "content_changed"
    assert wrong.status_code == 409
    assert wrong.json()["detail"]["code"] == "content_changed"
    assert accepted.status_code == 200
    assert accepted.json()["enabled"] is True

    # Owned, never-imported skills keep today's immediate enable path.
    local = await create_skill(data_layer, title="local")
    with acting_as(OWNER):
        local_enabled = await api.put(
            f"/project/skills/{local['id']}", json={"enabled": True}
        )
    assert local_enabled.status_code == 200
    assert local_enabled.json()["enabled"] is True


async def test_content_edit_disables_and_invalidates_the_review_hash(
    api: AsyncClient, data_layer: SQLAlchemyDataLayer
):
    source = await create_skill(data_layer, content="v1")
    await share_skill(api, source["id"], True)
    copy = (await add_copy(api, source["id"])).json()
    head = (await owner_versions(api, copy["id"], user=READER))[0]

    with acting_as(READER):
        enabled = await api.put(
            f"/project/skills/{copy['id']}",
            json={"enabled": True, "reviewedContentHash": head["contentHash"]},
        )
        assert enabled.status_code == 200

        edited = await api.put(
            f"/project/skills/{copy['id']}",
            json={
                "description": "edited description",
                "enabled": True,
                "reviewedContentHash": head["contentHash"],
            },
        )
        stale = await api.put(
            f"/project/skills/{copy['id']}",
            json={"enabled": True, "reviewedContentHash": head["contentHash"]},
        )

    assert edited.status_code == 200
    assert edited.json()["enabled"] is False
    assert edited.json()["version"] == 2
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "content_changed"

    current = (await owner_versions(api, copy["id"], user=READER))[0]
    with acting_as(READER):
        reviewed = await api.put(
            f"/project/skills/{copy['id']}",
            json={
                "enabled": True,
                "reviewedContentHash": current["contentHash"],
            },
        )
    assert reviewed.status_code == 200
    assert reviewed.json()["enabled"] is True


# ---- ownership, versions and capability fallbacks ----


async def test_owner_version_routes_are_owner_only(
    api: AsyncClient, data_layer: SQLAlchemyDataLayer
):
    skill = await create_skill(data_layer)
    version_id = (await data_layer.list_prompt_versions(skill["id"]))[0]["id"]

    mine = await owner_versions(api, skill["id"])
    assert mine[0]["createdBy"] == OWNER.id
    with acting_as(OWNER):
        snapshot = await api.get(f"/project/skills/{skill['id']}/versions/{version_id}")
    assert snapshot.status_code == 200
    assert snapshot.json()["content"] == "Alpha body."

    with acting_as(STRANGER):
        listing = await api.get(f"/project/skills/{skill['id']}/versions")
        single = await api.get(f"/project/skills/{skill['id']}/versions/{version_id}")
    assert listing.status_code == 404
    assert single.status_code == 404


async def test_unsupported_layer_returns_501(api: AsyncClient, monkeypatch):
    unsupported = AsyncMock(spec=BaseDataLayer)
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: unsupported)

    with acting_as(OWNER):
        share = await api.post(
            "/project/skills/anything/share", json={"isShared": True}
        )
        updates = await api.get("/project/skills/updates")
        versions = await api.get("/project/skills/anything/versions")
        add = await api.post("/project/skill/share/anything/add", json={})

    assert share.status_code == 501
    assert updates.status_code == 501
    assert versions.status_code == 501
    assert add.status_code == 501


async def test_skill_capability_flags(api: AsyncClient, data_layer, monkeypatch):
    monkeypatch.setattr(config.features, "skills", True)
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: data_layer)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/project/settings")
    assert response.status_code == 200
    body = response.json()
    assert body["skillsLibrary"] is True
    assert body["skillsSharing"] is True

    unsupported = AsyncMock(spec=BaseDataLayer)
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: unsupported)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        disabled = await client.get("/project/settings")
    assert disabled.json()["skillsLibrary"] is False
    assert disabled.json()["skillsSharing"] is False


async def test_deleting_a_shared_skill_removes_versions_and_notices(
    api: AsyncClient, data_layer: SQLAlchemyDataLayer
):
    source = await create_skill(data_layer)
    await share_skill(api, source["id"], True)
    await add_copy(api, source["id"])
    await update_row(data_layer, source, content="v2")

    with acting_as(READER):
        assert (await api.get("/project/skills/updates")).json() != []

    with acting_as(OWNER):
        deleted = await api.delete(f"/project/skills/{source['id']}")
    assert deleted.status_code == 200
    assert (await api.get(f"/project/skill/share/{source['id']}")).status_code == 404
    assert await data_layer.list_prompt_versions(source["id"]) == []

    with acting_as(READER):
        assert (await api.get("/project/skills/updates")).json() == []
