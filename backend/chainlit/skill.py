"""App-directory skills: Markdown instruction packages discovered from APP_ROOT/skills.

A skill is a Markdown file with a small frontmatter header. Its description is safe
to show to a model; its body is inert text until the application chooses to render it.
"""

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

import chainlit.config as chainlit_config

from ._utils import is_path_inside
from .step import Step

SKILLS_DIRNAME = "skills"
MAX_SKILLS = 200
MAX_BODY_BYTES = 64 * 1024
MAX_DESCRIPTION_BYTES = 1024
CATALOG_MAX_CHARS = 8 * 1024
SKILL_TOOL_NAME = "load_skill"

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class SkillError(ValueError):
    """Raised when a skill file is invalid or its path escapes the skills root."""


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    version: Optional[str]
    license: Optional[str]
    metadata: Dict[str, str]
    body: str
    path: Path

    def summary(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.summary(),
            "license": self.license,
            "metadata": dict(self.metadata),
            "path": str(self.path),
            "body": self.body,
        }


_cache: Optional[List[Skill]] = None


def _parse_scalar(value: str, path: Path) -> str:
    value = value.strip()
    if value in {"|", ">", "|-", ">-", "|+", ">+"}:
        raise SkillError(
            f"{path}: frontmatter block scalars ('|' or '>') are not supported"
        )
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_inline_metadata(path: Path, value: str) -> Dict[str, str]:
    if not (value.startswith("{") and value.endswith("}")):
        raise SkillError(f"{path}: 'metadata' must be a mapping")
    metadata: Dict[str, str] = {}
    for pair in value[1:-1].split(","):
        if not pair.strip():
            continue
        key, separator, item = pair.partition(":")
        if not separator or not key.strip():
            raise SkillError(f"{path}: invalid metadata entry: {pair.strip()!r}")
        metadata[key.strip()] = _parse_scalar(item, path)
    return metadata


def _parse_frontmatter(
    path: Path, text: str
) -> tuple[Dict[str, str], Dict[str, str], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\n") != "---":
        raise SkillError(f"{path}: missing frontmatter (expected a leading '---' line)")

    end = next(
        (i for i in range(1, len(lines)) if lines[i].rstrip("\n") == "---"), None
    )
    if end is None:
        raise SkillError(f"{path}: missing frontmatter terminator '---'")

    values: Dict[str, str] = {}
    metadata: Dict[str, str] = {}
    current_section: Optional[str] = None

    for raw_line in (line.rstrip("\n") for line in lines[1:end]):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue

        if raw_line[0].isspace():
            if current_section != "metadata":
                raise SkillError(
                    f"{path}: nested frontmatter is only supported under 'metadata': "
                    f"{raw_line.strip()!r}"
                )
            key, separator, value = raw_line.strip().partition(":")
            if not separator or not key.strip():
                raise SkillError(
                    f"{path}: invalid metadata entry: {raw_line.strip()!r}"
                )
            metadata[key.strip()] = _parse_scalar(value, path)
            continue

        current_section = None
        key, separator, value = raw_line.partition(":")
        if not separator:
            raise SkillError(
                f"{path}: invalid frontmatter line (expected 'key: value'): {raw_line!r}"
            )

        key = key.strip()
        if key == "metadata":
            current_section = "metadata"
            if value.strip():
                metadata.update(_parse_inline_metadata(path, value.strip()))
        else:
            values[key] = _parse_scalar(value, path)

    return values, metadata, "".join(lines[end + 1 :])


def _load_skill(root: Path, relative: str) -> Skill:
    rel = PurePosixPath(relative)
    if rel.is_absolute():
        raise SkillError(f"{relative}: absolute skill paths are not allowed")
    if any(part == ".." for part in rel.parts):
        raise SkillError(
            f"{relative}: path traversal is not allowed; skills must stay inside {root}"
        )

    path = root
    for part in rel.parts:
        path = path / part
        if path.is_symlink():
            raise SkillError(f"{path}: symlinks are not allowed in skills")

    if not is_path_inside(path, root):
        raise SkillError(f"{path}: skill path resolves outside {root}")
    if not path.is_file():
        raise SkillError(f"{path}: skill path is not a file")

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise SkillError(f"{path}: cannot read skill file: {error}") from error

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    values, metadata, body = _parse_frontmatter(path, text)

    name = values.get("name", "")
    if not name:
        raise SkillError(f"{path}: missing required frontmatter field 'name'")
    if not _NAME_PATTERN.fullmatch(name):
        raise SkillError(
            f"{path}: invalid skill name {name!r}; expected {_NAME_PATTERN.pattern}"
        )

    description = values.get("description", "")
    if not description:
        raise SkillError(f"{path}: missing required frontmatter field 'description'")
    description_size = len(description.encode("utf-8"))
    if description_size > MAX_DESCRIPTION_BYTES:
        raise SkillError(
            f"{path}: description is {description_size} bytes; "
            f"maximum is {MAX_DESCRIPTION_BYTES}"
        )

    body_size = len(body.encode("utf-8"))
    if body_size > MAX_BODY_BYTES:
        raise SkillError(
            f"{path}: body is {body_size} bytes; maximum is {MAX_BODY_BYTES}"
        )

    return Skill(
        id=rel.as_posix(),
        name=name,
        description=description,
        version=values.get("version") or None,
        license=values.get("license") or None,
        metadata=metadata,
        body=body,
        path=path,
    )


def _relative_skill_paths(root: Path) -> List[str]:
    if not root.exists():
        return []
    if root.is_symlink():
        raise SkillError(f"{root}: skills directory must not be a symlink")
    if not root.is_dir():
        raise SkillError(f"{root}: skills path is not a directory")

    candidates: List[str] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.name.startswith("."):
            continue
        if entry.is_symlink():
            raise SkillError(f"{entry}: symlinks are not allowed in skills")
        if entry.is_dir():
            if (entry / "SKILL.md").is_file():
                candidates.append(f"{entry.name}/SKILL.md")
        elif entry.suffix == ".md":
            candidates.append(entry.name)

    if len(candidates) > MAX_SKILLS:
        raise SkillError(
            f"{root}: found {len(candidates)} skills; maximum is {MAX_SKILLS}"
        )
    return candidates


def load_skills(force: bool = False) -> List[Skill]:
    """Load and validate every app skill, failing fast on the first invalid one."""
    global _cache
    if _cache is not None and not force:
        return _cache

    root = Path(chainlit_config.APP_ROOT) / SKILLS_DIRNAME
    skills = [_load_skill(root, relative) for relative in _relative_skill_paths(root)]

    by_name: Dict[str, Skill] = {}
    for skill in skills:
        existing = by_name.get(skill.name)
        if existing:
            raise SkillError(
                f"{skill.path}: duplicate skill name {skill.name!r} "
                f"(already defined in {existing.path})"
            )
        by_name[skill.name] = skill

    _cache = skills
    return _cache


def _find_skill(name: str) -> Skill:
    for skill in load_skills():
        if skill.name == name:
            return skill
    raise KeyError(f"Unknown skill: {name!r}")


def list_skills() -> List[Dict[str, Any]]:
    """Return catalog entries for every app skill, sorted by file name."""
    return [skill.summary() for skill in load_skills()]


def get_skill(name: str) -> Dict[str, Any]:
    """Return one skill's metadata and body."""
    return _find_skill(name).to_dict()


def skills_catalog() -> str:
    """Return compact 'name: description' lines, capped for model context."""
    catalog = ""
    for skill in load_skills():
        line = f"{skill.name}: {skill.description}"
        candidate = f"{catalog}\n{line}" if catalog else line
        if len(candidate) <= CATALOG_MAX_CHARS:
            catalog = candidate
            continue

        available = CATALOG_MAX_CHARS - len(catalog) - (1 if catalog else 0) - 3
        if available > 0:
            prefix = f"{catalog}\n" if catalog else ""
            catalog = f"{prefix}{line[:available]}..."
        break
    return catalog


def render_skill(name: str) -> str:
    """Return a skill's body wrapped in provenance delimiters."""
    skill = _find_skill(name)
    body = skill.body.strip("\n")
    return (
        f"--- BEGIN SKILL {skill.name} (id: {skill.id}, source: {skill.path}) ---\n"
        f"{body}\n"
        f"--- END SKILL {skill.name} ---"
    )


def skill_tool() -> Dict[str, Any]:
    """Return a provider-neutral ``load_skill`` tool spec and async handler.

    The handler emits a Chainlit step named after the loaded skill, so the thread
    shows which skill fired and the loaded body stays inspectable.
    """

    async def handler(name: str) -> str:
        try:
            skill = _find_skill(name)
        except KeyError:
            available = ", ".join(item.name for item in load_skills()) or "none"
            return f"Unknown skill {name!r}. Available skills: {available}."

        # The loader rejects oversize bodies; re-check so a cached registry can
        # never hand a model more than the cap.
        body_size = len(skill.body.encode("utf-8"))
        if body_size > MAX_BODY_BYTES:
            return (
                f"Skill {skill.name!r} is unavailable: its body is {body_size} "
                f"bytes; maximum is {MAX_BODY_BYTES}."
            )

        rendered = render_skill(skill.name)
        async with Step(name=f"skill: {skill.name}", type="tool") as step:
            step.input = name
            step.output = rendered
        return rendered

    return {
        "name": SKILL_TOOL_NAME,
        "description": (
            "Load the full instructions for one skill by name. Use it only when a "
            "skill in the catalog matches the user's request."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact skill name from the skills catalog.",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "handler": handler,
    }
