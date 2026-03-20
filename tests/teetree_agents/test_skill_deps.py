from pathlib import Path

import pytest

from teetree.agents.skill_bundle import (
    find_skill_md,
    parse_skill_requires,
    resolve_dependencies,
    resolve_skill_bundle,
)

SKILL_WITH_REQUIRES = """\
---
name: t3-code
description: Writing code.
requires:
    - t3-workspace
metadata:
    version: 0.0.1
---

# Writing Code
"""

SKILL_NO_REQUIRES = """\
---
name: t3-workspace
description: Workspace management.
metadata:
    version: 0.0.1
---

# Workspace
"""

SKILL_NO_FRONTMATTER = "# Just a heading\n"

SKILL_MULTIPLE_REQUIRES = """\
---
name: t3-contribute
description: Contribute.
requires:
    - t3-retro
    - t3-ship
metadata:
    version: 0.0.1
---
"""


def test_parse_skill_requires_extracts_list() -> None:
    assert parse_skill_requires(SKILL_WITH_REQUIRES) == ["t3-workspace"]


def test_parse_skill_requires_returns_empty_when_no_requires() -> None:
    assert parse_skill_requires(SKILL_NO_REQUIRES) == []


def test_parse_skill_requires_returns_empty_when_no_frontmatter() -> None:
    assert parse_skill_requires(SKILL_NO_FRONTMATTER) == []


def test_parse_skill_requires_multiple_deps() -> None:
    assert parse_skill_requires(SKILL_MULTIPLE_REQUIRES) == ["t3-retro", "t3-ship"]


def _write_skill(skills_dir: Path, name: str, content: str) -> None:
    (skills_dir / name).mkdir(parents=True)
    (skills_dir / name / "SKILL.md").write_text(content, encoding="utf-8")


def test_resolve_dependencies_follows_requires(tmp_path: Path) -> None:
    _write_skill(tmp_path, "t3-code", SKILL_WITH_REQUIRES)
    _write_skill(tmp_path, "t3-workspace", SKILL_NO_REQUIRES)

    result = resolve_dependencies(["t3-code"], skills_dir=tmp_path)

    assert result == ["t3-workspace", "t3-code"]


def test_resolve_dependencies_transitive(tmp_path: Path) -> None:
    _write_skill(tmp_path, "t3-contribute", SKILL_MULTIPLE_REQUIRES)
    _write_skill(tmp_path, "t3-retro", SKILL_NO_REQUIRES)
    _write_skill(
        tmp_path,
        "t3-ship",
        """\
---
name: t3-ship
description: Ship.
requires:
    - t3-workspace
metadata:
    version: 0.0.1
---
""",
    )
    _write_skill(tmp_path, "t3-workspace", SKILL_NO_REQUIRES)

    result = resolve_dependencies(["t3-contribute"], skills_dir=tmp_path)

    assert result == ["t3-retro", "t3-workspace", "t3-ship", "t3-contribute"]


def test_resolve_dependencies_deduplicates(tmp_path: Path) -> None:
    _write_skill(tmp_path, "t3-code", SKILL_WITH_REQUIRES)
    _write_skill(tmp_path, "t3-workspace", SKILL_NO_REQUIRES)

    result = resolve_dependencies(["t3-workspace", "t3-code"], skills_dir=tmp_path)

    assert result == ["t3-workspace", "t3-code"]


def test_resolve_dependencies_handles_cycle(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "a",
        "---\nname: a\ndescription: A.\nrequires:\n  - b\nmetadata:\n  version: 0.0.1\n---\n",
    )
    _write_skill(
        tmp_path,
        "b",
        "---\nname: b\ndescription: B.\nrequires:\n  - a\nmetadata:\n  version: 0.0.1\n---\n",
    )

    result = resolve_dependencies(["a"], skills_dir=tmp_path)

    assert result == ["b", "a"]


def test_resolve_dependencies_missing_skill_dir(tmp_path: Path) -> None:
    result = resolve_dependencies(["nonexistent"], skills_dir=tmp_path)

    assert result == ["nonexistent"]


def test_resolve_dependencies_follows_file_path(tmp_path: Path) -> None:
    skill_file = tmp_path / "my-overlay" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text(
        "---\nname: my-overlay\ndescription: Overlay.\nrequires:\n  - t3-workspace\nmetadata:\n  version: 0.0.1\n---\n",
        encoding="utf-8",
    )
    _write_skill(tmp_path, "t3-workspace", SKILL_NO_REQUIRES)

    result = resolve_dependencies([str(skill_file)], skills_dir=tmp_path)

    assert result == ["t3-workspace", str(skill_file)]


def test_resolve_skill_bundle_includes_dependencies(tmp_path: Path) -> None:
    _write_skill(tmp_path, "test-driven-development", SKILL_WITH_REQUIRES.replace("t3-code", "tdd"))
    _write_skill(tmp_path, "t3-workspace", SKILL_NO_REQUIRES)

    bundle = resolve_skill_bundle(
        phase="coding",
        overlay_skill_metadata={},
        delegation_map_path=Path("references/skill-delegation.md"),
        skills_dir=tmp_path,
    )

    assert "t3-workspace" in bundle
    assert bundle.index("t3-workspace") < bundle.index("test-driven-development")


# --- find_skill_md edge cases ---


def test_find_skill_md_returns_none_for_missing_skill_md_in_existing_dir(tmp_path: Path) -> None:
    """When path ends with SKILL.md, parent dir exists, but the file doesn't."""
    missing = tmp_path / "my-skill" / "SKILL.md"
    missing.parent.mkdir()
    # The file does not exist, but the parent dir does.
    result = find_skill_md(str(missing), tmp_path)
    assert result is None


# --- resolve_dependencies edge case: name already in resolved ---


def test_resolve_dependencies_does_not_duplicate_when_dep_already_resolved(tmp_path: Path) -> None:
    """A dependency that is also explicitly listed should appear only once."""
    _write_skill(tmp_path, "t3-code", SKILL_WITH_REQUIRES)
    _write_skill(tmp_path, "t3-workspace", SKILL_NO_REQUIRES)

    # t3-workspace is both a dependency of t3-code and explicitly listed after it.
    # resolve_dependencies should not add t3-workspace twice.
    result = resolve_dependencies(["t3-code", "t3-workspace"], skills_dir=tmp_path)
    assert result.count("t3-workspace") == 1
    assert result.count("t3-code") == 1


# --- resolve_skill_bundle edge case: duplicate in ordered list ---


def test_resolve_skill_bundle_deduplicates_resolved_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When resolve_dependencies returns duplicates, resolve_skill_bundle deduplicates."""
    # Force resolve_dependencies to return a list with duplicates
    monkeypatch.setattr(
        "teetree.agents.skill_bundle.resolve_dependencies",
        lambda skills, **_kw: ["a", "b", "a", "b", "c"],
    )

    bundle = resolve_skill_bundle(
        phase="coding",
        overlay_skill_metadata={},
        delegation_map_path=Path("references/skill-delegation.md"),
        skills_dir=tmp_path,
    )

    assert bundle == ["a", "b", "c"]
