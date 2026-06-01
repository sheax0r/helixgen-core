"""Lightweight checks on every `.claude/skills/<name>/SKILL.md` in the repo.

Mirrors what the Claude Code skill loader needs to succeed: a YAML frontmatter
block with `name` and `description` keys, combined size ≤ 1024 chars
(Anthropic skill-frontmatter convention).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


SKILLS_ROOT = Path(__file__).resolve().parents[1] / ".claude" / "skills"


def _skill_files() -> list[Path]:
    if not SKILLS_ROOT.is_dir():
        return []
    return sorted(SKILLS_ROOT.glob("*/SKILL.md"))


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Return {key: value} from a `---` … `---` YAML frontmatter block at start of file.

    Hand-rolled (no PyYAML dep) — the format is simple enough: single-line
    `key: value` pairs.
    """
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    if m is None:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


@pytest.mark.parametrize("skill_path", _skill_files(), ids=lambda p: p.parent.name)
def test_skill_has_name_and_description(skill_path: Path) -> None:
    text = skill_path.read_text()
    fm = _parse_frontmatter(text)
    assert "name" in fm and fm["name"], f"{skill_path}: missing or empty `name`"
    assert "description" in fm and fm["description"], (
        f"{skill_path}: missing or empty `description`"
    )


@pytest.mark.parametrize("skill_path", _skill_files(), ids=lambda p: p.parent.name)
def test_skill_frontmatter_size_under_1024(skill_path: Path) -> None:
    text = skill_path.read_text()
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    assert m is not None, f"{skill_path}: no frontmatter block"
    # combined size = name + description (the two fields the loader cares about)
    fm = _parse_frontmatter(text)
    combined = (fm.get("name", "") + fm.get("description", ""))
    assert len(combined) <= 1024, (
        f"{skill_path}: name+description is {len(combined)} chars, > 1024 limit"
    )


@pytest.mark.parametrize("skill_path", _skill_files(), ids=lambda p: p.parent.name)
def test_skill_name_matches_directory(skill_path: Path) -> None:
    fm = _parse_frontmatter(skill_path.read_text())
    assert fm.get("name") == skill_path.parent.name, (
        f"{skill_path}: frontmatter name {fm.get('name')!r} ≠ "
        f"directory name {skill_path.parent.name!r}"
    )
