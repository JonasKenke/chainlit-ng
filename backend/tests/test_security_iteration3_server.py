"""Server regressions for iteration-3 prompt/skill business-logic fixes."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError

import chainlit.server as server_mod
from chainlit.auth import get_current_user
from chainlit.config import config
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.prompt_versioning import PromptTitleTooLarge, PromptValidationError
from chainlit.state_store.memory import MemoryStateStore
from chainlit.types import PromptDict
from chainlit.user import PersistedUser

NOW = "2026-01-01T00:00:00Z"
OWNER = PersistedUser(id="owner-1", identifier="owner@example.test", createdAt=NOW)
READER = PersistedUser(id="reader-1", identifier="reader@example.test", createdAt=NOW)
STRANGER = PersistedUser(
    id="stranger-1", identifier="stranger@example.test", createdAt=NOW
)
USERS = {user.identifier: user for user in (OWNER, READER, STRANGER)}


class RecordingStore(MemoryStateStore):
    def __init__(self) -> None:
        super().__init__(prefix=f"iteration3-server-{uuid.uuid4().hex}")
        self.keys: list[str] = []

    def lock(self, key: str, *, ttl: float = 10, wait: float = 0):
        self.keys.append(key)
        return super().lock(key, ttl=ttl, wait=wait)


def prompt_row(
    title: str = "title",
    content: str = "content",
    *,
    user_id: str = OWNER.id,
    shared: bool = False,
) -> PromptDict:
    return PromptDict(
        id=str(uuid.uuid4()),
        userId=user_id,
        title=title,
        content=content,
        isShared=shared,
        createdAt=NOW,
        updatedAt=NOW,
    )


def skill_row(
    title: str = "alpha",
    content: str = "body",
    *,
    user_id: str = OWNER.id,
    shared: bool = False,
) -> PromptDict:
    return PromptDict(
        id=str(uuid.uuid4()),
        userId=user_id,
        title=title,
        description=f"{title} description",
        content=content,
        isShared=shared,
        kind="skill",
        enabled=False,
        createdAt=NOW,
        updatedAt=NOW,
    )


async def shared_prompt(layer: SQLAlchemyDataLayer, title: str, content: str):
    row = await layer.create_prompt(prompt_row(title=title, content=content))
    return await layer.update_prompt(
        {**row, "isShared": True, "sharedAt": NOW, "sharedVersion": 1, "updatedAt": NOW}
    )


async def shared_skill(layer: SQLAlchemyDataLayer, title: str, content: str):
    row = await layer.create_skill(skill_row(title=title, content=content))
    return await layer.update_skill(
        {**row, "isShared": True, "sharedAt": NOW, "sharedVersion": 1, "updatedAt": NOW}
    )


def headers(user: PersistedUser | None) -> dict[str, str]:
    return {"X-Audit-User": user.identifier} if user else {}


@pytest.fixture
async def store(monkeypatch: pytest.MonkeyPatch):
    value = RecordingStore()
    monkeypatch.setattr(server_mod, "get_state_store", lambda: value)
    yield value
    await value.close()


@pytest.fixture
async def data_layer(tmp_path: Path):
    layer = SQLAlchemyDataLayer(
        conninfo=f"sqlite+aiosqlite:///{tmp_path / 'iteration3-server.sqlite'}",
        storage_provider=None,
    )
    yield layer
    await layer.close()


@pytest.fixture
async def api(monkeypatch: pytest.MonkeyPatch, data_layer, store, tmp_path: Path):
    monkeypatch.setattr(config.features, "prompt_gallery", True)
    monkeypatch.setattr(config.features, "skills", True)
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: data_layer)
    monkeypatch.setattr("chainlit.config.APP_ROOT", str(tmp_path))

    async def current_user(request: Request):
        identifier = request.headers.get("X-Audit-User")
        return USERS.get(identifier) if identifier else None

    app = FastAPI()
    app.add_exception_handler(
        RequestValidationError,
        server_mod.request_validation_exception_handler,  # type: ignore[arg-type]
    )
    app.include_router(server_mod.skills_router)
    app.include_router(server_mod.router)
    app.dependency_overrides[get_current_user] = current_user
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=True),
        base_url="http://test",
        timeout=30,
    ) as client:
        yield client
    app.dependency_overrides.pop(get_current_user, None)


async def test_prompt_title_boundary_and_surrogate_validation(api, data_layer):
    exact = await api.post(
        "/project/prompts",
        json={"title": "😀" * 256, "content": "body"},
        headers=headers(OWNER),
    )
    assert exact.status_code == 200, exact.text

    over = await api.post(
        "/project/prompts",
        json={"title": "😀" * 256 + "a", "content": "body"},
        headers=headers(OWNER),
    )
    assert over.status_code == 422

    raw = json.dumps({"title": "title", "content": "\ud800"}).encode()
    surrogate = await api.post(
        "/project/prompts",
        content=raw,
        headers={**headers(OWNER), "Content-Type": "application/json"},
    )
    assert surrogate.status_code == 422
    assert len(await data_layer.list_prompts(OWNER.id)) == 1


async def test_prompt_typed_data_layer_errors_map_to_http(api, data_layer, monkeypatch):
    monkeypatch.setattr(
        data_layer,
        "create_prompt",
        AsyncMock(side_effect=PromptValidationError("invalid Unicode")),
    )
    invalid = await api.post(
        "/project/prompts",
        json={"title": "title", "content": "body"},
        headers=headers(OWNER),
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "validation_error"

    monkeypatch.setattr(
        data_layer,
        "create_prompt",
        AsyncMock(side_effect=PromptTitleTooLarge("title too large")),
    )
    large = await api.post(
        "/project/prompts",
        json={"title": "title", "content": "body"},
        headers=headers(OWNER),
    )
    assert large.status_code == 413
    assert large.json()["detail"]["code"] == "title_too_large"


async def test_skill_typed_errors_map_to_http(api, data_layer, monkeypatch):
    original_create = data_layer.create_skill
    monkeypatch.setattr(
        data_layer,
        "create_skill",
        AsyncMock(side_effect=PromptValidationError("invalid Unicode")),
    )
    invalid = await api.post(
        "/project/skills",
        json={"name": "alpha", "description": "d", "content": "b"},
        headers=headers(OWNER),
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "validation_error"
    monkeypatch.setattr(data_layer, "create_skill", original_create)

    monkeypatch.setattr(data_layer, "list_skills", AsyncMock(return_value=[]))
    first = await api.post(
        "/project/skills",
        json={"name": "same-name", "description": "d", "content": "b"},
        headers=headers(OWNER),
    )
    second = await api.post(
        "/project/skills",
        json={"name": "same-name", "description": "d", "content": "b"},
        headers=headers(OWNER),
    )
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "name_taken"


async def test_prompt_diff_work_budget_returns_413(api, data_layer):
    source = await shared_prompt(layer=data_layer, title="diff", content="a\n" * 2001)
    old_version = (await data_layer.list_prompt_versions(source["id"]))[0]["id"]
    await data_layer.update_prompt(
        {**source, "content": "a\n" * 2000, "updatedAt": NOW}
    )
    response = await api.get(
        f"/project/prompts/{source['id']}/diff",
        params={"from": "local", "to": old_version},
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "diff_too_large"


async def test_copy_and_sync_validate_legacy_oversize_titles(api, data_layer):
    source = await shared_prompt(layer=data_layer, title="source", content="v1")
    with_copy = await api.post(
        f"/project/prompt/share/{source['id']}/add", headers=headers(READER)
    )
    copy_id = with_copy.json()["id"]
    await api.put(
        f"/project/prompts/{copy_id}",
        json={"content": "local edit"},
        headers=headers(READER),
    )

    huge_title = "T" * 1025
    await data_layer.execute_sql(
        'UPDATE prompts SET "title"=:title WHERE "id"=:id',
        {"title": huge_title, "id": source["id"]},
    )
    await data_layer.execute_sql(
        'UPDATE prompt_versions SET "title"=:title WHERE "promptId"=:id',
        {"title": huge_title, "id": source["id"]},
    )

    copied = await api.post(
        f"/project/prompt/share/{source['id']}/add", headers=headers(STRANGER)
    )
    synced = await api.post(
        f"/project/prompts/{copy_id}/sync", json={}, headers=headers(READER)
    )
    assert copied.status_code == 413
    assert copied.json()["detail"]["code"] == "title_too_large"
    assert synced.status_code == 413
    assert synced.json()["detail"]["code"] == "title_too_large"
    assert await data_layer.list_prompts(STRANGER.id) == []
    assert (await data_layer.get_prompt(copy_id))["content"] == "local edit"


async def test_concurrent_prompt_add_copy_has_one_winner(api, data_layer):
    source = await shared_prompt(layer=data_layer, title="source", content="v1")
    responses = await asyncio.gather(
        *(
            api.post(
                f"/project/prompt/share/{source['id']}/add", headers=headers(READER)
            )
            for _ in range(12)
        )
    )
    assert [response.status_code for response in responses].count(200) == 1
    assert [response.status_code for response in responses].count(409) == 11
    copies = await data_layer.execute_sql(
        'SELECT "id" FROM prompts WHERE "userId"=:user AND "sourcePromptId"=:source',
        {"user": READER.id, "source": source["id"]},
    )
    assert len(copies) == 1


async def test_concurrent_skill_add_copy_has_one_winner(api, data_layer):
    source = await shared_skill(layer=data_layer, title="source-skill", content="v1")
    responses = await asyncio.gather(
        *(
            api.post(
                f"/project/skill/share/{source['id']}/add", headers=headers(READER)
            )
            for _ in range(12)
        )
    )
    assert [response.status_code for response in responses].count(200) == 1
    assert [response.status_code for response in responses].count(409) == 11
    copies = await data_layer.execute_sql(
        'SELECT "id" FROM prompts WHERE "userId"=:user AND "sourcePromptId"=:source',
        {"user": READER.id, "source": source["id"]},
    )
    assert len(copies) == 1


async def test_concurrent_skill_create_cannot_exceed_cap(api, data_layer):
    for index in range(server_mod.MAX_SKILLS - 1):
        await data_layer.create_skill(
            skill_row(title=f"cap-{index:03d}", user_id=READER.id)
        )
    responses = await asyncio.gather(
        *(
            api.post(
                "/project/skills",
                json={"name": f"race-{index:03d}", "description": "d", "content": "b"},
                headers=headers(READER),
            )
            for index in range(12)
        )
    )
    assert [response.status_code for response in responses].count(201) == 1
    assert [response.status_code for response in responses].count(400) == 11
    assert len(await data_layer.list_skills(READER.id)) == server_mod.MAX_SKILLS

    imported = await api.post(
        "/project/skills/import",
        json={
            "markdown": "---\nname: over-cap\ndescription: d\n---\nb",
            "imported_from": "x.md",
        },
        headers=headers(READER),
    )
    assert imported.status_code == 400

    source = await shared_skill(layer=data_layer, title="source-at-cap", content="v1")
    add_copy = await api.post(
        f"/project/skill/share/{source['id']}/add", headers=headers(READER)
    )
    assert add_copy.status_code == 400
    assert all(
        item.get("sourcePromptId") != source["id"]
        for item in await data_layer.list_skills(READER.id)
    )


async def test_prompt_unshare_winner_blocks_inflight_sync(api, data_layer, monkeypatch):
    source = await shared_prompt(layer=data_layer, title="source", content="v1")
    copied = await api.post(
        f"/project/prompt/share/{source['id']}/add", headers=headers(READER)
    )
    copy_id = copied.json()["id"]
    await data_layer.update_prompt(
        {**source, "content": "POST-UNSHARE-SECRET", "updatedAt": NOW}
    )

    original_update = data_layer.update_prompt
    entered = asyncio.Event()
    release = asyncio.Event()
    paused = False

    async def paused_update(row):
        nonlocal paused
        if row["id"] == source["id"] and not row["isShared"] and not paused:
            paused = True
            entered.set()
            await release.wait()
        return await original_update(row)

    monkeypatch.setattr(data_layer, "update_prompt", paused_update)
    unshare = asyncio.create_task(
        api.post(
            f"/project/prompts/{source['id']}/share",
            json={"isShared": False},
            headers=headers(OWNER),
        )
    )
    done, _ = await asyncio.wait([unshare], timeout=0.5)
    if done:
        raise AssertionError(
            f"unshare finished before barrier: {unshare.result().text}"
        )
    await asyncio.wait_for(entered.wait(), timeout=5)
    sync = asyncio.create_task(
        api.post(f"/project/prompts/{copy_id}/sync", json={}, headers=headers(READER))
    )
    await asyncio.sleep(0.05)
    assert not sync.done()
    release.set()
    unshare_response = await unshare
    sync_response = await sync

    assert unshare_response.status_code == 200
    assert sync_response.status_code == 409
    assert sync_response.json()["detail"]["code"] == "source_unavailable"
    assert (await data_layer.get_prompt(copy_id))["content"] == "v1"
    assert len(await data_layer.list_prompt_versions(copy_id)) == 1


async def test_skill_unshare_winner_blocks_inflight_sync(api, data_layer, monkeypatch):
    source = await shared_skill(layer=data_layer, title="source-skill", content="v1")
    copied = await api.post(
        f"/project/skill/share/{source['id']}/add", headers=headers(READER)
    )
    copy_id = copied.json()["id"]
    await data_layer.update_skill(
        {**source, "content": "POST-UNSHARE-SECRET", "updatedAt": NOW}
    )

    original_update = data_layer.update_skill
    entered = asyncio.Event()
    release = asyncio.Event()
    paused = False

    async def paused_update(row):
        nonlocal paused
        if row["id"] == source["id"] and not row["isShared"] and not paused:
            paused = True
            entered.set()
            await release.wait()
        return await original_update(row)

    monkeypatch.setattr(data_layer, "update_skill", paused_update)
    unshare = asyncio.create_task(
        api.post(
            f"/project/skills/{source['id']}/share",
            json={"isShared": False},
            headers=headers(OWNER),
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    sync = asyncio.create_task(
        api.post(
            f"/project/skills/{copy_id}/update-from-source",
            json={},
            headers=headers(READER),
        )
    )
    await asyncio.sleep(0.05)
    assert not sync.done()
    release.set()
    unshare_response = await unshare
    sync_response = await sync

    assert unshare_response.status_code == 200
    assert sync_response.status_code == 409
    assert sync_response.json()["detail"]["code"] == "source_unavailable"
    assert (await data_layer.get_skill(copy_id, READER.id))["content"] == "v1"
    assert len(await data_layer.list_prompt_versions(copy_id)) == 1


async def test_share_rejects_legacy_invalid_prompt_and_skill_titles(api, data_layer):
    prompt = await data_layer.create_prompt(prompt_row(title="legacy"))
    await data_layer.execute_sql(
        'UPDATE prompts SET "title"=:title WHERE "id"=:id',
        {"title": "T" * 1025, "id": prompt["id"]},
    )
    prompt_share = await api.post(
        f"/project/prompts/{prompt['id']}/share",
        json={"isShared": True},
        headers=headers(OWNER),
    )
    assert prompt_share.status_code == 413
    assert prompt_share.json()["detail"]["code"] == "title_too_large"
    assert (await data_layer.get_prompt(prompt["id"]))["isShared"] is False

    skill = await data_layer.create_skill(skill_row(title="legacy-skill"))
    await data_layer.execute_sql(
        'UPDATE prompts SET "title"=:title WHERE "id"=:id',
        {"title": "Invalid_Name", "id": skill["id"]},
    )
    skill_share = await api.post(
        f"/project/skills/{skill['id']}/share",
        json={"isShared": True},
        headers=headers(OWNER),
    )
    assert skill_share.status_code == 400
    assert (await data_layer.get_skill(skill["id"], OWNER.id))["isShared"] is False


async def test_multi_key_mutation_locks_are_sorted(store):
    async with server_mod._mutation_locks(["b", "a", "b"], "test"):
        pass
    assert store.keys == ["a", "b"]
