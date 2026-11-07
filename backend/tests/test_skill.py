from pathlib import Path

import pytest

import chainlit as cl
import chainlit.config as chainlit_config
import chainlit.skill as skill_module


@pytest.fixture(autouse=True)
def reset_skill_cache():
    skill_module._cache = None
    yield
    skill_module._cache = None


@pytest.fixture
def skills_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(chainlit_config, "APP_ROOT", str(tmp_path))
    root = tmp_path / "skills"
    root.mkdir()
    return root


def write_skill(
    path: Path,
    name: str = "alpha",
    description: str = "Alpha description",
    body: str = "Alpha body.",
    extra_frontmatter: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n"
        f"{extra_frontmatter}---\n{body}",
        encoding="utf-8",
    )


def test_loads_both_layouts_and_public_api(skills_root: Path):
    write_skill(
        skills_root / "alpha" / "SKILL.md",
        body="Alpha body.",
        extra_frontmatter="version: 1.2.0\nlicense: MIT\nmetadata:\n  owner: team-a\n",
    )
    write_skill(
        skills_root / "beta.md",
        name="beta",
        description="'Beta description'",
        body="Beta body.",
    )

    skills = skill_module.load_skills()
    assert [skill.name for skill in skills] == ["alpha", "beta"]

    assert cl.list_skills() == [
        {
            "id": "alpha/SKILL.md",
            "name": "alpha",
            "description": "Alpha description",
            "version": "1.2.0",
        },
        {
            "id": "beta.md",
            "name": "beta",
            "description": "Beta description",
            "version": None,
        },
    ]

    alpha = cl.get_skill("alpha")
    assert alpha["body"] == "Alpha body."
    assert alpha["license"] == "MIT"
    assert alpha["metadata"] == {"owner": "team-a"}
    assert alpha["path"] == str(skills_root / "alpha" / "SKILL.md")

    catalog = cl.skills_catalog()
    assert "alpha: Alpha description" in catalog
    assert "beta: Beta description" in catalog
    assert len(catalog) <= skill_module.CATALOG_MAX_CHARS

    rendered = cl.render_skill("alpha")
    assert rendered.startswith("--- BEGIN SKILL alpha")
    assert "Alpha body." in rendered
    assert alpha["path"] in rendered
    assert rendered.endswith("--- END SKILL alpha ---")


def test_empty_skills_directory(skills_root: Path):
    assert cl.list_skills() == []
    assert cl.skills_catalog() == ""


def test_invalid_name(skills_root: Path):
    write_skill(skills_root / "bad.md", name="Bad_Name")

    with pytest.raises(
        skill_module.SkillError, match=r"bad\.md: invalid skill name 'Bad_Name'"
    ):
        skill_module.load_skills()


def test_duplicate_name(skills_root: Path):
    write_skill(skills_root / "one.md", name="same")
    write_skill(skills_root / "two.md", name="same")

    with pytest.raises(
        skill_module.SkillError,
        match=r"two\.md: duplicate skill name 'same' \(already defined in .*one\.md\)",
    ):
        skill_module.load_skills()


def test_path_traversal_rejected(skills_root: Path):
    with pytest.raises(skill_module.SkillError, match="path traversal is not allowed"):
        skill_module._load_skill(skills_root, "../escape.md")


def test_absolute_path_rejected(skills_root: Path):
    with pytest.raises(
        skill_module.SkillError, match="absolute skill paths are not allowed"
    ):
        skill_module._load_skill(skills_root, "/etc/passwd")


def test_symlink_rejected(skills_root: Path):
    target = skills_root / "real.md"
    write_skill(target)
    (skills_root / "link.md").symlink_to(target)

    with pytest.raises(
        skill_module.SkillError, match=r"link\.md: symlinks are not allowed"
    ):
        skill_module.load_skills()


def test_oversize_body_rejected(skills_root: Path):
    write_skill(skills_root / "big.md", body="x" * (skill_module.MAX_BODY_BYTES + 1))

    with pytest.raises(
        skill_module.SkillError,
        match=rf"big\.md: body is \d+ bytes; maximum is {skill_module.MAX_BODY_BYTES}",
    ):
        skill_module.load_skills()


def test_oversize_description_rejected(skills_root: Path):
    write_skill(
        skills_root / "long.md",
        description="x" * (skill_module.MAX_DESCRIPTION_BYTES + 1),
    )

    with pytest.raises(
        skill_module.SkillError,
        match=rf"long\.md: description is \d+ bytes; "
        rf"maximum is {skill_module.MAX_DESCRIPTION_BYTES}",
    ):
        skill_module.load_skills()


def test_missing_frontmatter(skills_root: Path):
    (skills_root / "plain.md").write_text("# no frontmatter\n", encoding="utf-8")

    with pytest.raises(
        skill_module.SkillError, match=r"plain\.md: missing frontmatter"
    ):
        skill_module.load_skills()


def test_missing_description(skills_root: Path):
    (skills_root / "nodesc.md").write_text(
        "---\nname: nodesc\n---\nbody", encoding="utf-8"
    )

    with pytest.raises(
        skill_module.SkillError,
        match=r"nodesc\.md: missing required frontmatter field 'description'",
    ):
        skill_module.load_skills()


def test_too_many_skills(skills_root: Path):
    for index in range(skill_module.MAX_SKILLS + 1):
        write_skill(skills_root / f"skill-{index:03}.md", name=f"skill-{index:03}")

    with pytest.raises(
        skill_module.SkillError,
        match=rf"maximum is {skill_module.MAX_SKILLS}",
    ):
        skill_module.load_skills()


def test_catalog_is_capped(skills_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(skill_module, "CATALOG_MAX_CHARS", 120)
    for index in range(3):
        write_skill(
            skills_root / f"skill-{index}.md",
            name=f"skill-{index}",
            description="x" * 100,
        )

    catalog = cl.skills_catalog()

    assert len(catalog) <= 120
    assert catalog.startswith("skill-0: ")
    assert catalog.endswith("...")


def test_unknown_skill_raises_key_error(skills_root: Path):
    with pytest.raises(KeyError, match="Unknown skill"):
        cl.get_skill("missing")
