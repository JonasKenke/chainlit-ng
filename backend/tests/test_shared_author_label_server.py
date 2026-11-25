"""HTTP integration tests for stored shared-author labels."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

import chainlit.server as server_mod
from chainlit.auth import get_current_user
from chainlit.config import config
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.data.storage_clients.base import BaseStorageClient
from chainlit.types import PromptDict
from chainlit.user import PersistedUser

OWNER = PersistedUser(
    id="owner-id",
    identifier="owner@example.test",
    createdAt="2026-01-01T00:00:00Z",
    display_name="Ada Lovelace",
)
READER = PersistedUser(
    id="reader-id",
    identifier="reader@example.test",
    createdAt="2026-01-01T00:00:00Z",
    display_name="Grace Hopper",
)


@pytest.fixture(autouse=True)
def author_config(monkeypatch):
    monkeypatch.setattr(config.features, "prompt_gallery", True)
    monkeypatch.setattr(config.features, "skills", True)
    monkeypatch.setattr(config.code, "shared_author_label", None)


@pytest.fixture
async def data_layer(tmp_path: Path):
    layer = SQLAlchemyDataLayer(
        conninfo=f"sqlite+aiosqlite:///{tmp_path / 'shared-author-server.sqlite'}",
        storage_provider=AsyncMock(spec=BaseStorageClient),
    )
    yield layer
    await layer.close()


@pytest.fixture
async def api(data_layer, monkeypatch):
    app = FastAPI()
    app.include_router(server_mod.skills_router)
    app.include_router(server_mod.router)
    monkeypatch.setattr(server_mod, "get_data_layer", lambda: data_layer)
    app.dependency_overrides[get_current_user] = lambda: OWNER
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        yield app, client
    app.dependency_overrides.pop(get_current_user, None)


@contextmanager
def acting_as(app: FastAPI, user: PersistedUser):
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        yield
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous


def prompt_row(prompt_id: str = "prompt-1", **overrides: Any) -> PromptDict:
    return cast(
        PromptDict,
        {
            "id": prompt_id,
            "userId": OWNER.id,
            "title": "Prompt",
            "content": "Prompt body",
            "isShared": False,
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            **overrides,
        },
    )


def skill_row(skill_id: str = "skill-1", **overrides: Any) -> PromptDict:
    return cast(
        PromptDict,
        {
            **prompt_row(
                skill_id,
                title="shared-skill",
                content="Skill body",
                kind="skill",
                description="Skill description",
                enabled=False,
            ),
            **overrides,
        },
    )


@pytest.mark.parametrize(
    ("kind", "create", "share_path", "public_path"),
    [
        (
            "prompt",
            lambda layer: layer.create_prompt(prompt_row()),
            "/project/prompts/prompt-1/share",
            "/project/prompt/share/prompt-1",
        ),
        (
            "skill",
            lambda layer: layer.create_skill(skill_row()),
            "/project/skills/skill-1/share",
            "/project/skill/share/skill-1",
        ),
    ],
)
async def test_default_label_is_stored_and_publicly_projected(
    api, data_layer, monkeypatch, kind, create, share_path, public_path
):
    _, client = api
    await create(data_layer)
    with acting_as(api[0], OWNER):
        shared = await client.post(share_path, json={"isShared": True})
    assert shared.status_code == 200
    assert shared.json()[kind]["authorLabel"] == "Ada Lovelace"

    public = await client.get(public_path)
    assert public.status_code == 200
    assert public.json()["authorLabel"] == "Ada Lovelace"
    assert "userId" not in public.json()
    assert "createdBy" not in public.json()


@pytest.mark.parametrize(
    ("create", "share_path", "public_path"),
    [
        (
            lambda layer: layer.create_prompt(prompt_row()),
            "/project/prompts/prompt-1/share",
            "/project/prompt/share/prompt-1",
        ),
        (
            lambda layer: layer.create_skill(skill_row()),
            "/project/skills/skill-1/share",
            "/project/skill/share/skill-1",
        ),
    ],
)
async def test_callback_override_and_opt_out(
    api, data_layer, monkeypatch, create, share_path, public_path
):
    _, client = api
    await create(data_layer)

    async def label_callback(user):
        assert user is OWNER
        return "  Curated\n Author  "

    monkeypatch.setattr(config.code, "shared_author_label", label_callback)
    with acting_as(api[0], OWNER):
        shared = await client.post(share_path, json={"isShared": True})
    assert shared.status_code == 200
    assert (await client.get(public_path)).json()["authorLabel"] == "Curated Author"

    monkeypatch.setattr(config.code, "shared_author_label", lambda user: None)
    with acting_as(api[0], OWNER):
        reshared = await client.post(share_path, json={"isShared": True})
    assert reshared.status_code == 200
    assert (await client.get(public_path)).json()["authorLabel"] is None


@pytest.mark.parametrize(
    ("create", "share_path", "public_path", "stored"),
    [
        (
            lambda layer: layer.create_prompt(prompt_row()),
            "/project/prompts/prompt-1/share",
            "/project/prompt/share/prompt-1",
            lambda layer: layer.get_prompt("prompt-1"),
        ),
        (
            lambda layer: layer.create_skill(skill_row()),
            "/project/skills/skill-1/share",
            "/project/skill/share/skill-1",
            lambda layer: layer.get_skill("skill-1", OWNER.id),
        ),
    ],
)
async def test_unshare_clears_label_and_hides_resource(
    api, data_layer, create, share_path, public_path, stored
):
    _, client = api
    await create(data_layer)
    with acting_as(api[0], OWNER):
        assert (
            await client.post(share_path, json={"isShared": True})
        ).status_code == 200
        unshared = await client.post(share_path, json={"isShared": False})
    assert unshared.status_code == 200
    assert (await stored(data_layer))["authorLabel"] is None
    assert (await client.get(public_path)).status_code == 404


@pytest.mark.parametrize(
    ("create", "share_path", "public_path"),
    [
        (
            lambda layer: layer.create_prompt(prompt_row()),
            "/project/prompts/prompt-1/share",
            "/project/prompt/share/prompt-1",
        ),
        (
            lambda layer: layer.create_skill(skill_row()),
            "/project/skills/skill-1/share",
            "/project/skill/share/skill-1",
        ),
    ],
)
async def test_reshare_recomputes_label(
    api, data_layer, monkeypatch, create, share_path, public_path
):
    _, client = api
    await create(data_layer)
    with acting_as(api[0], OWNER):
        assert (
            await client.post(share_path, json={"isShared": True})
        ).status_code == 200
    assert (await client.get(public_path)).json()["authorLabel"] == "Ada Lovelace"

    monkeypatch.setattr(config.code, "shared_author_label", lambda user: "New Label")
    with acting_as(api[0], OWNER):
        assert (
            await client.post(share_path, json={"isShared": True})
        ).status_code == 200
    assert (await client.get(public_path)).json()["authorLabel"] == "New Label"


@pytest.mark.parametrize(
    ("create", "make_shared", "public_path"),
    [
        (
            lambda layer: layer.create_prompt(prompt_row()),
            lambda layer, row: layer.update_prompt({**row, "isShared": True}),
            "/project/prompt/share/prompt-1",
        ),
        (
            lambda layer: layer.create_skill(skill_row()),
            lambda layer, row: layer.update_skill({**row, "isShared": True}),
            "/project/skill/share/skill-1",
        ),
    ],
)
async def test_legacy_shared_row_without_label_returns_null(
    api, data_layer, create, make_shared, public_path
):
    _, client = api
    row = await create(data_layer)
    await make_shared(data_layer, row)
    public = await client.get(public_path)
    assert public.status_code == 200
    assert public.json()["authorLabel"] is None


@pytest.mark.parametrize(
    ("kind", "create", "public_path"),
    [
        (
            "prompt",
            lambda layer: layer.create_prompt(prompt_row(isShared=True)),
            "/project/prompt/share/prompt-1",
        ),
        (
            "skill",
            lambda layer: layer.create_skill(skill_row(isShared=True)),
            "/project/skill/share/skill-1",
        ),
    ],
)
async def test_legacy_custom_row_without_label_key_keeps_old_projection(
    api, data_layer, monkeypatch, kind, create, public_path
):
    _, client = api
    row = dict(await create(data_layer))
    row.pop("authorLabel", None)
    if kind == "prompt":
        monkeypatch.setattr(data_layer, "get_prompt", AsyncMock(return_value=row))
    else:
        monkeypatch.setattr(data_layer, "get_shared_skill", AsyncMock(return_value=row))

    public = await client.get(public_path)
    assert public.status_code == 200
    if kind == "prompt":
        assert "authorLabel" not in public.json()
    else:
        assert public.json()["authorLabel"] == "Chainlit NG user"


@pytest.mark.parametrize(
    ("create", "share_path"),
    [
        (
            lambda layer: layer.create_prompt(prompt_row()),
            "/project/prompts/prompt-1/share",
        ),
        (
            lambda layer: layer.create_skill(skill_row()),
            "/project/skills/skill-1/share",
        ),
    ],
)
async def test_request_cannot_supply_author_label(api, data_layer, create, share_path):
    _, client = api
    await create(data_layer)
    with acting_as(api[0], OWNER):
        response = await client.post(
            share_path,
            json={"isShared": True, "authorLabel": "Attacker Label"},
        )
    assert response.status_code == 200
    assert (
        response.json()["prompt" if "prompts" in share_path else "skill"]["authorLabel"]
        == "Ada Lovelace"
    )


async def test_local_prompt_copy_does_not_inherit_source_label(api, data_layer):
    _, client = api
    await data_layer.create_prompt(
        prompt_row(authorLabel="Source Author", isShared=True)
    )
    with acting_as(api[0], READER):
        copied = await client.post("/project/prompt/share/prompt-1/add")
    assert copied.status_code == 200
    stored = await data_layer.get_prompt(copied.json()["id"])
    assert stored is not None
    assert stored["authorLabel"] is None


async def test_local_skill_copy_does_not_inherit_source_label(api, data_layer):
    _, client = api
    await data_layer.create_skill(skill_row(authorLabel="Source Author", isShared=True))
    with acting_as(api[0], READER):
        copied = await client.post("/project/skill/share/skill-1/add")
    assert copied.status_code == 200
    stored = await data_layer.get_skill(copied.json()["id"], READER.id)
    assert stored is not None
    assert stored["authorLabel"] is None


async def test_ordinary_prompt_edit_preserves_stored_label(api, data_layer):
    _, client = api
    await data_layer.create_prompt(
        prompt_row(authorLabel="Stored Author", isShared=True)
    )
    with acting_as(api[0], OWNER):
        response = await client.put(
            "/project/prompts/prompt-1", json={"title": "Edited"}
        )
    assert response.status_code == 200
    assert (await data_layer.get_prompt("prompt-1"))["authorLabel"] == "Stored Author"


async def test_ordinary_skill_edit_preserves_stored_label(api, data_layer):
    _, client = api
    await data_layer.create_skill(skill_row(authorLabel="Stored Author", isShared=True))
    with acting_as(api[0], OWNER):
        response = await client.put(
            "/project/skills/skill-1", json={"description": "Edited"}
        )
    assert response.status_code == 200
    assert (await data_layer.get_skill("skill-1", OWNER.id))[
        "authorLabel"
    ] == "Stored Author"


async def test_prompt_sync_and_detach_preserve_local_label(api, data_layer):
    _, client = api
    source = await data_layer.create_prompt(
        prompt_row("source", authorLabel="Source Author", isShared=True)
    )
    source_version = (await data_layer.list_prompt_versions(source["id"]))[0]["id"]
    copy = await data_layer.create_prompt(
        prompt_row(
            "copy",
            content="Local edited body",
            authorLabel="Local Author",
            sourcePromptId=source["id"],
            pinnedVersionId=source_version,
            pinnedContentHash=source["contentHash"],
        )
    )
    with acting_as(api[0], OWNER):
        synced = await client.post(
            "/project/prompts/copy/sync",
            json={"versionId": source_version, "confirm": True},
        )
        detached = await client.post("/project/prompts/copy/detach")
    assert synced.status_code == 200
    assert detached.status_code == 200
    stored = await data_layer.get_prompt(copy["id"])
    assert stored["authorLabel"] == "Local Author"
    assert stored["sourcePromptId"] is None


async def test_skill_source_update_and_detach_preserve_local_label(api, data_layer):
    _, client = api
    source = await data_layer.create_skill(
        skill_row(
            "source-skill", userId=READER.id, authorLabel="Source Author", isShared=True
        )
    )
    source_version = (await data_layer.list_prompt_versions(source["id"]))[0]["id"]
    copy = await data_layer.create_skill(
        skill_row(
            "copy-skill",
            authorLabel="Local Author",
            enabled=True,
            sourcePromptId=source["id"],
            pinnedVersionId=source_version,
            pinnedContentHash=source["contentHash"],
        )
    )
    with acting_as(api[0], OWNER):
        updated = await client.post(
            "/project/skills/copy-skill/update-from-source",
            json={"versionId": source_version},
        )
        assert updated.status_code == 200
        stored = await data_layer.get_skill(copy["id"], OWNER.id)
        assert stored["authorLabel"] == "Local Author"
        assert stored["enabled"] is False
        reviewed = stored["contentHash"]
        enabled = await client.put(
            "/project/skills/copy-skill",
            json={"enabled": True, "reviewedContentHash": reviewed},
        )
        assert enabled.status_code == 200
        detached = await client.post("/project/skills/copy-skill/detach")
    assert detached.status_code == 200
    stored = await data_layer.get_skill(copy["id"], OWNER.id)
    assert stored["authorLabel"] == "Local Author"
    assert stored["sourcePromptId"] is None
