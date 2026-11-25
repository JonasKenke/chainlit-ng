"""Regression tests for iteration-3 prompt/skill validation and diff limits."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

import chainlit.prompt_versioning as prompt_versioning
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.skill import SkillError, parse_skill_markdown
from chainlit.types import (
    CreatePromptRequest,
    CreateSkillRequest,
    ImportSkillRequest,
    PromptDict,
    UpdatePromptRequest,
)

NOW = "2026-01-01T00:00:00Z"
TITLE_LIMIT = 1024
DIFF_WORK_LIMIT = 4_000_000


def prompt_row(
    title: str = "title",
    content: str = "content",
    *,
    prompt_id: str | None = None,
) -> PromptDict:
    return PromptDict(
        id=prompt_id or str(uuid.uuid4()),
        userId="user-1",
        title=title,
        content=content,
        isShared=False,
        createdAt=NOW,
        updatedAt=NOW,
    )


def skill_row(
    title: str = "alpha",
    content: str = "body",
    *,
    skill_id: str | None = None,
) -> PromptDict:
    return PromptDict(
        id=skill_id or str(uuid.uuid4()),
        userId="user-1",
        title=title,
        description="description",
        content=content,
        isShared=False,
        kind="skill",
        enabled=False,
        createdAt=NOW,
        updatedAt=NOW,
    )


@pytest.fixture
async def data_layer(tmp_path: Path):
    layer = SQLAlchemyDataLayer(
        conninfo=f"sqlite+aiosqlite:///{tmp_path / 'iteration3.sqlite'}",
        storage_provider=None,
    )
    yield layer
    await layer.close()


def test_prompt_title_limit_counts_utf8_bytes_at_exact_boundary():
    prompt_versioning.enforce_title_limit("a" * TITLE_LIMIT)
    prompt_versioning.enforce_title_limit("😀" * (TITLE_LIMIT // 4))


@pytest.mark.parametrize("title", ["a" * (TITLE_LIMIT + 1), "😀" * 256 + "a"])
def test_prompt_title_limit_rejects_one_byte_over(title: str):
    with pytest.raises(prompt_versioning.PromptTitleTooLarge) as raised:
        prompt_versioning.enforce_title_limit(title)
    assert raised.value.code == "title_too_large"
    assert str(TITLE_LIMIT) in str(raised.value)


@pytest.mark.parametrize("value", ["\ud800", "before\udfffafter"])
@pytest.mark.parametrize("field", ["title", "content"])
def test_prompt_validation_rejects_lone_surrogates(value: str, field: str):
    helper = (
        prompt_versioning.enforce_title_limit
        if field == "title"
        else prompt_versioning.enforce_content_limit
    )
    with pytest.raises(prompt_versioning.PromptValidationError) as raised:
        helper(value)
    assert raised.value.code == "validation_error"
    assert field in str(raised.value)


def test_prompt_validation_preserves_valid_astral_unicode():
    prompt_versioning.enforce_title_limit("😀")
    prompt_versioning.enforce_content_limit("😀")


def test_prompt_content_limit_behavior_is_unchanged():
    content = "é" * (prompt_versioning.MAX_PROMPT_CONTENT_BYTES // 2)
    prompt_versioning.enforce_content_limit(content)
    with pytest.raises(prompt_versioning.PromptContentTooLarge):
        prompt_versioning.enforce_content_limit(content + "a")


def test_prompt_request_models_reject_surrogates_and_oversize_titles():
    with pytest.raises(ValidationError):
        CreatePromptRequest(title="\ud800", content="body")
    with pytest.raises(ValidationError):
        CreatePromptRequest(title="title", content="\ud800")
    with pytest.raises(ValidationError):
        CreatePromptRequest(title="a" * (TITLE_LIMIT + 1), content="body")
    with pytest.raises(ValidationError):
        UpdatePromptRequest(title="\ud800")

    assert CreatePromptRequest(title="😀", content="😀").content == "😀"


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (CreateSkillRequest, {"name": "\ud800", "description": "d", "content": "b"}),
        (CreateSkillRequest, {"name": "n", "description": "\ud800", "content": "b"}),
        (CreateSkillRequest, {"name": "n", "description": "d", "content": "\ud800"}),
        (
            ImportSkillRequest,
            {
                "markdown": "---\nname: n\ndescription: d\n---\n\ud800",
                "imported_from": "x.md",
            },
        ),
        (
            ImportSkillRequest,
            {
                "markdown": "---\nname: n\ndescription: d\n---\nb",
                "imported_from": "\ud800",
            },
        ),
    ],
)
def test_skill_request_models_reject_lone_surrogates(model, payload):
    with pytest.raises(ValidationError):
        model(**payload)


def test_skill_markdown_parser_reports_surrogate_as_skill_error():
    markdown = "---\nname: alpha\ndescription: description\n---\n\ud800"
    with pytest.raises(SkillError, match="Unicode"):
        parse_skill_markdown(markdown)


def test_diff_allows_exact_work_boundary():
    rows, truncated = prompt_versioning.build_diff(
        "a\nb\n", "a\nc\n", max_rows=10, max_bytes=10_000, max_work_cells=4
    )
    assert truncated is False
    assert rows


@pytest.mark.parametrize(
    ("old", "new", "limit"),
    [
        ("a\nb\nc\n", "a\nb\n", 5),
        ("a\n" * 2001, "a\n" * 2000, DIFF_WORK_LIMIT),
    ],
)
def test_diff_rejects_work_over_budget_before_matching(
    old: str, new: str, limit: int, monkeypatch: pytest.MonkeyPatch
):
    def unexpected_matcher(*args, **kwargs):
        raise AssertionError("SequenceMatcher must not run over the work budget")

    monkeypatch.setattr(
        prompt_versioning.difflib, "SequenceMatcher", unexpected_matcher
    )
    with pytest.raises(prompt_versioning.PromptDiffTooLarge) as raised:
        prompt_versioning.build_diff(old, new, max_work_cells=limit)
    assert raised.value.code == "diff_too_large"
    assert str(limit) in str(raised.value)


async def test_sql_prompt_title_limit_rejects_before_head_or_version_write(data_layer):
    exact = "😀" * (TITLE_LIMIT // 4)
    created = await data_layer.create_prompt(prompt_row(title=exact))
    assert created["title"] == exact

    with pytest.raises(prompt_versioning.PromptTitleTooLarge):
        await data_layer.create_prompt(prompt_row(title=exact + "a"))

    with pytest.raises(prompt_versioning.PromptTitleTooLarge):
        await data_layer.update_prompt({**created, "title": exact + "a"})

    prompts = await data_layer.list_prompts("user-1")
    assert [item["id"] for item in prompts] == [created["id"]]
    assert len(await data_layer.list_prompt_versions(created["id"])) == 1


async def test_sql_prompt_surrogate_rejection_leaves_no_rows(data_layer):
    with pytest.raises(prompt_versioning.PromptValidationError):
        await data_layer.create_prompt(prompt_row(title="\ud800"))
    with pytest.raises(prompt_versioning.PromptValidationError):
        await data_layer.create_prompt(prompt_row(content="\ud800"))
    assert await data_layer.list_prompts("user-1") == []


async def test_sql_skill_name_conflict_has_dedicated_code(data_layer):
    await data_layer.list_skills("user-1")
    first = await data_layer.create_skill(skill_row(title="same-name"))
    with pytest.raises(prompt_versioning.SkillNameConflict) as raised:
        await data_layer.create_skill(skill_row(title="same-name"))
    assert raised.value.code == "name_taken"
    assert len(await data_layer.list_skills("user-1")) == 1
    assert (await data_layer.get_skill(first["id"], "user-1")) is not None


async def test_sql_skill_other_integrity_errors_are_not_name_conflicts(data_layer):
    await data_layer.list_skills("user-1")
    row = skill_row(title="first", skill_id="duplicate-id")
    await data_layer.create_skill(row)
    with pytest.raises(RuntimeError, match="Could not store the skill"):
        await data_layer.create_skill(
            skill_row(title="different-name", skill_id="duplicate-id")
        )


async def test_sql_concurrent_skill_name_conflict_is_typed(data_layer):
    await data_layer.list_skills("user-1")
    rows = [skill_row(title="raced-name") for _ in range(2)]
    results = await asyncio.gather(
        *(data_layer.create_skill(cast(PromptDict, row)) for row in rows),
        return_exceptions=True,
    )
    assert sum(isinstance(item, dict) for item in results) == 1
    conflicts = [item for item in results if isinstance(item, Exception)]
    assert len(conflicts) == 1
    assert isinstance(conflicts[0], prompt_versioning.SkillNameConflict)
    assert len(await data_layer.list_skills("user-1")) == 1
