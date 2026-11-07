"""SQLAlchemy per-user skill persistence and migration tests."""

import uuid
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.data.storage_clients.base import BaseStorageClient
from chainlit.types import PromptDict


def skill_row(user_id: str, title: str = "alpha", skill_id: str | None = None):
    return PromptDict(
        id=skill_id or str(uuid.uuid4()),
        userId=user_id,
        title=title,
        content=f"{title} body",
        isShared=False,
        createdAt="2024-01-01T00:00:00Z",
        updatedAt="2024-01-01T00:00:00Z",
        kind="skill",
        description=f"{title} description",
        enabled=True,
        importedFrom=None,
        importedAt=None,
        contentHash=None,
    )


@pytest.fixture
async def db_file(tmp_path: Path) -> Path:
    return tmp_path / "skills.sqlite"


@pytest.fixture
async def skill_data_layer(db_file: Path, mock_storage_client: BaseStorageClient):
    layer = SQLAlchemyDataLayer(
        conninfo=f"sqlite+aiosqlite:///{db_file}",
        storage_provider=mock_storage_client,
    )
    yield layer
    await layer.close()


async def test_existing_prompts_table_is_migrated_in_place(
    db_file: Path, skill_data_layer: SQLAlchemyDataLayer
):
    """An app that already has the old prompts table gains skill columns safely."""
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
                ('prompt-1', 'user-1', 'Old prompt', 'Old body', 0,
                 '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')
                """
            )
        )
    await engine.dispose()

    created = await skill_data_layer.create_skill(skill_row("user-1"))
    assert created["kind"] == "skill"

    prompt = await skill_data_layer.get_prompt("prompt-1")
    assert prompt is not None
    assert prompt["content"] == "Old body"

    skills = await skill_data_layer.list_skills("user-1")
    assert [skill["id"] for skill in skills] == [created["id"]]

    columns = await skill_data_layer.execute_sql("PRAGMA table_info(prompts)", {})
    assert isinstance(columns, list)
    names = {row["name"] for row in columns}
    assert {
        "kind",
        "description",
        "enabled",
        "importedFrom",
        "importedAt",
        "contentHash",
    } <= names


async def test_prompt_only_use_does_not_add_skill_columns(
    skill_data_layer: SQLAlchemyDataLayer,
):
    """The flag-off path leaves the prompts schema exactly as it was."""
    prompt = PromptDict(
        id="prompt-1",
        userId="user-a",
        title="prompt",
        content="body",
        isShared=False,
        createdAt="2024-01-01T00:00:00Z",
        updatedAt="2024-01-01T00:00:00Z",
    )
    await skill_data_layer.create_prompt(prompt)

    columns = await skill_data_layer.execute_sql("PRAGMA table_info(prompts)", {})
    assert isinstance(columns, list)
    assert "kind" not in {row["name"] for row in columns}


async def test_skill_crud_is_owner_scoped(skill_data_layer: SQLAlchemyDataLayer):
    mine = skill_row("user-a", title="mine")
    theirs = skill_row("user-b", title="theirs")
    await skill_data_layer.create_skill(mine)
    await skill_data_layer.create_skill(theirs)

    assert [s["id"] for s in await skill_data_layer.list_skills("user-a")] == [
        mine["id"]
    ]
    assert await skill_data_layer.list_skills("user-b") == [theirs]

    assert await skill_data_layer.get_skill(theirs["id"], "user-a") is None
    stored = await skill_data_layer.get_skill(mine["id"], "user-a")
    assert stored is not None
    assert stored["title"] == "mine"

    updated = cast(
        PromptDict,
        {**mine, "title": "renamed", "updatedAt": "2024-06-01T00:00:00Z"},
    )
    await skill_data_layer.update_skill(updated)
    renamed = await skill_data_layer.get_skill(mine["id"], "user-a")
    assert renamed is not None
    assert renamed["title"] == "renamed"

    assert await skill_data_layer.delete_skill(mine["id"], "user-b") is False
    assert await skill_data_layer.get_skill(mine["id"], "user-a") is not None
    assert await skill_data_layer.delete_skill(mine["id"], "user-a") is True
    assert await skill_data_layer.get_skill(mine["id"], "user-a") is None


async def test_update_cannot_move_a_skill_to_another_user(
    skill_data_layer: SQLAlchemyDataLayer,
):
    mine = skill_row("user-a")
    await skill_data_layer.create_skill(mine)

    stolen = cast(PromptDict, {**mine, "userId": "user-b", "title": "stolen"})
    await skill_data_layer.update_skill(stolen)

    stored = await skill_data_layer.get_skill(mine["id"], "user-a")
    assert stored is not None
    assert stored["title"] == mine["title"]
    assert await skill_data_layer.get_skill(mine["id"], "user-b") is None


async def test_prompts_and_skills_do_not_leak_into_each_other(
    skill_data_layer: SQLAlchemyDataLayer,
):
    prompt = PromptDict(
        id="prompt-1",
        userId="user-a",
        title="alpha",
        content="prompt body",
        isShared=False,
        createdAt="2024-01-01T00:00:00Z",
        updatedAt="2024-01-01T00:00:00Z",
    )
    await skill_data_layer.create_prompt(prompt)
    skill = skill_row("user-a", title="alpha")
    await skill_data_layer.create_skill(skill)

    assert await skill_data_layer.get_skill("prompt-1", "user-a") is None
    assert [s["id"] for s in await skill_data_layer.list_skills("user-a")] == [
        skill["id"]
    ]
    assert [p["id"] for p in await skill_data_layer.list_prompts("user-a")] == [
        "prompt-1"
    ]
