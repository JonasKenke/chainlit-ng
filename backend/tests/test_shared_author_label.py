"""Shared author label resolution and SQLAlchemy persistence tests."""

import logging
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from chainlit import callbacks, set_shared_author_label
from chainlit.config import ChainlitConfig
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.data.storage_clients.base import BaseStorageClient
from chainlit.share_author import resolve_shared_author_label
from chainlit.types import PromptDict
from chainlit.user import User


@pytest.fixture(autouse=True)
def author_config(
    test_config: ChainlitConfig, monkeypatch: pytest.MonkeyPatch
) -> ChainlitConfig:
    monkeypatch.setattr("chainlit.share_author.config", test_config)
    monkeypatch.setattr(test_config.code, "shared_author_label", None)
    return test_config


@pytest_asyncio.fixture
async def data_layer(tmp_path: Path):
    layer = SQLAlchemyDataLayer(
        conninfo=f"sqlite+aiosqlite:///{tmp_path / 'author_labels.sqlite'}",
        storage_provider=AsyncMock(spec=BaseStorageClient),
    )
    yield layer
    await layer.close()


def prompt_row(**overrides: Any) -> PromptDict:
    return cast(
        PromptDict,
        {
            "id": "prompt-1",
            "userId": "user-1",
            "title": "Prompt",
            "content": "Prompt body",
            "isShared": False,
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-01T00:00:00Z",
            **overrides,
        },
    )


def skill_row(**overrides: Any) -> PromptDict:
    return cast(
        PromptDict,
        {
            **prompt_row(
                id="skill-1",
                title="Skill",
                content="Skill body",
                kind="skill",
                description="Skill description",
                enabled=False,
            ),
            **overrides,
        },
    )


async def test_default_prefers_display_name():
    user = User(
        identifier="person@example.com",
        display_name="  Ada Lovelace  ",
        metadata={"email": "other@example.com"},
    )

    assert await resolve_shared_author_label(user) == "Ada Lovelace"


async def test_default_falls_back_to_metadata_email():
    user = User(identifier="opaque-id", metadata={"email": "ada@example.com"})

    assert await resolve_shared_author_label(user) == "ada@example.com"


@pytest.mark.parametrize("key", ["preferred_username", "name"])
async def test_default_falls_back_to_metadata_name(key: str):
    user = User(identifier="opaque-id", metadata={key: "ada"})

    assert await resolve_shared_author_label(user) == "ada"


async def test_default_falls_back_to_email_like_identifier():
    user = User(identifier="ada@example.com")

    assert await resolve_shared_author_label(user) == "ada@example.com"


@pytest.mark.parametrize("identifier", ["opaque-id", "ada@", "@example.com"])
async def test_default_omits_non_email_identifier(identifier: str):
    assert await resolve_shared_author_label(User(identifier=identifier)) is None


async def test_default_omits_label_without_user():
    assert await resolve_shared_author_label(None) is None


async def test_default_skips_blank_candidates():
    user = User(
        identifier="opaque-id",
        display_name=" \n ",
        metadata={"email": "", "preferred_username": "\t", "name": None},
    )

    assert await resolve_shared_author_label(user) is None


async def test_sync_callback_overrides_default(author_config: ChainlitConfig):
    user = User(identifier="ada@example.com", display_name="Ada")
    seen = []

    @set_shared_author_label
    def author_label(authenticated_user: User | None) -> str | None:
        seen.append(authenticated_user)
        return "Analytical Engine Team"

    assert author_config.code.shared_author_label is author_label
    assert callbacks.config.code.shared_author_label is author_label
    assert await resolve_shared_author_label(user) == "Analytical Engine Team"
    assert seen == [user]


async def test_async_callback_receives_unauthenticated_user(
    author_config: ChainlitConfig,
):
    seen = []

    @set_shared_author_label
    async def author_label(authenticated_user: User | None) -> str | None:
        seen.append(authenticated_user)
        return "Anonymous contributor"

    assert await resolve_shared_author_label(None) == "Anonymous contributor"
    assert seen == [None]


@pytest.mark.parametrize("value", [None, "", " \n\t "])
async def test_callback_can_opt_out(author_config: ChainlitConfig, value):
    @set_shared_author_label
    def author_label(_user: User | None):
        return value

    assert (
        await resolve_shared_author_label(
            User(identifier="ada@example.com", display_name="Ada")
        )
        is None
    )


async def test_callback_failure_fails_closed_without_logging_pii(
    author_config: ChainlitConfig, caplog: pytest.LogCaptureFixture
):
    @set_shared_author_label
    def author_label(_user: User | None):
        raise RuntimeError("private-token")

    with caplog.at_level(logging.WARNING, logger="chainlit"):
        result = await resolve_shared_author_label(
            User(identifier="ada@example.com", display_name="Ada")
        )

    assert result is None
    assert "private-token" not in caplog.text
    assert "ada@example.com" not in caplog.text


async def test_callback_label_is_normalized_to_one_safe_line(
    author_config: ChainlitConfig,
):
    @set_shared_author_label
    def author_label(_user: User | None):
        return "  Ada\u200b\n\tLovelace\u202e\x00  "

    assert await resolve_shared_author_label(None) == "Ada Lovelace"


async def test_callback_label_is_capped(author_config: ChainlitConfig):
    @set_shared_author_label
    def author_label(_user: User | None):
        return "x" * 250

    assert await resolve_shared_author_label(None) == "x" * 200


async def test_default_label_is_sanitized():
    user = User(identifier="opaque-id", display_name="Ada\nLovelace\u200b")

    assert await resolve_shared_author_label(user) == "Ada Lovelace"


async def test_prompt_author_label_round_trip_and_clear(data_layer):
    created = await data_layer.create_prompt(prompt_row(authorLabel="Ada"))
    assert created["authorLabel"] == "Ada"

    stored = await data_layer.get_prompt("prompt-1")
    assert stored is not None
    assert stored["authorLabel"] == "Ada"

    await data_layer.update_prompt(
        prompt_row(authorLabel="Grace", updatedAt="2024-02-01T00:00:00Z")
    )
    stored = await data_layer.get_prompt("prompt-1")
    assert stored is not None
    assert stored["authorLabel"] == "Grace"

    await data_layer.update_prompt(
        prompt_row(authorLabel=None, updatedAt="2024-03-01T00:00:00Z")
    )
    stored = await data_layer.get_prompt("prompt-1")
    assert stored is not None
    assert stored["authorLabel"] is None


async def test_skill_author_label_round_trip_and_clear(data_layer):
    created = await data_layer.create_skill(skill_row(authorLabel="Ada"))
    assert created["authorLabel"] == "Ada"

    stored = await data_layer.get_skill("skill-1", "user-1")
    assert stored is not None
    assert stored["authorLabel"] == "Ada"

    await data_layer.update_skill(
        skill_row(
            authorLabel="Grace",
            isShared=True,
            updatedAt="2024-02-01T00:00:00Z",
        )
    )
    stored = await data_layer.get_shared_skill("skill-1")
    assert stored is not None
    assert stored["authorLabel"] == "Grace"

    await data_layer.update_skill(
        skill_row(authorLabel=None, updatedAt="2024-03-01T00:00:00Z")
    )
    stored = await data_layer.get_skill("skill-1", "user-1")
    assert stored is not None
    assert stored["authorLabel"] is None


async def test_prompt_without_author_label_remains_valid(data_layer):
    await data_layer.create_prompt(prompt_row())

    stored = await data_layer.get_prompt("prompt-1")
    assert stored is not None
    assert stored["authorLabel"] is None


async def test_skill_without_author_label_remains_valid(data_layer):
    await data_layer.create_skill(skill_row())

    stored = await data_layer.get_skill("skill-1", "user-1")
    assert stored is not None
    assert stored["authorLabel"] is None


@pytest.mark.parametrize(
    "ensure_method", ["_ensure_prompt_versioning_locked", "_ensure_skills_columns"]
)
async def test_legacy_schema_gains_nullable_author_label(
    data_layer, tmp_path: Path, ensure_method: str
):
    db_file = tmp_path / "legacy.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as connection:
        await connection.execute(
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
        await connection.execute(
            text(
                """
                INSERT INTO prompts
                ("id", "userId", "title", "content", "isShared", "createdAt", "updatedAt")
                VALUES ('prompt-1', 'user-1', 'Legacy', 'Legacy body', 0,
                        '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')
                """
            )
        )
    await engine.dispose()

    legacy_layer = SQLAlchemyDataLayer(
        conninfo=f"sqlite+aiosqlite:///{db_file}",
        storage_provider=AsyncMock(spec=BaseStorageClient),
    )
    try:
        await getattr(legacy_layer, ensure_method)()
        columns = await legacy_layer.execute_sql("PRAGMA table_info(prompts)", {})
        assert isinstance(columns, list)
        author_column = next(row for row in columns if row["name"] == "authorLabel")
        assert author_column["type"].upper() == "TEXT"
        assert author_column["notnull"] == 0

        stored = await legacy_layer.get_prompt("prompt-1")
        assert stored is not None
        assert stored["authorLabel"] is None
    finally:
        await legacy_layer.close()
