from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from teetree.agents.skill_bundle import (
    find_skill_md,
    parse_skill_requires,
    resolve_dependencies,
    resolve_skill_bundle,
)
from teetree.skill_loading import SkillLoadingPolicy

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
    _write_skill(tmp_path, "t3-code", SKILL_WITH_REQUIRES)
    _write_skill(tmp_path, "t3-workspace", SKILL_NO_REQUIRES)

    bundle = resolve_skill_bundle(
        phase="coding",
        overlay_skill_metadata={},
        delegation_map_path=Path("references/skill-delegation.md"),
        skills_dir=tmp_path,
    )

    assert "t3-workspace" in bundle
    assert bundle.index("t3-workspace") < bundle.index("t3-code")


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
        "teetree.skill_loading.resolve_dependencies",
        lambda skills, **_kw: ["a", "b", "a", "b", "c"],
    )

    bundle = resolve_skill_bundle(
        phase="coding",
        overlay_skill_metadata={},
        delegation_map_path=Path("references/skill-delegation.md"),
        skills_dir=tmp_path,
    )

    assert bundle == ["a", "b", "c"]


# --- SkillLoadingPolicy coverage ---


def test_parse_skill_requires_unclosed_frontmatter() -> None:
    assert parse_skill_requires("---\nrequires:\n  - foo\n") == []


def test_agent_launch_explicit_phase_and_skills_raises() -> None:
    policy = SkillLoadingPolicy(skills_dir=Path("/nonexistent"))
    with pytest.raises(ValueError, match="cannot be used together"):
        policy.select_for_agent_launch(
            cwd=Path("/tmp"),
            overlay_skill_metadata={},
            task="",
            ticket_status="",
            explicit_phase="coding",
            explicit_skills=["t3-code"],
            overlay_active=False,
        )


def test_agent_launch_unknown_explicit_phase_raises() -> None:
    policy = SkillLoadingPolicy(skills_dir=Path("/nonexistent"))
    with pytest.raises(ValueError, match="Unknown phase"):
        policy.select_for_agent_launch(
            cwd=Path("/tmp"),
            overlay_skill_metadata={},
            task="",
            ticket_status="",
            explicit_phase="nonsense",
            explicit_skills=[],
            overlay_active=False,
        )


def test_agent_launch_valid_explicit_phase(tmp_path: Path) -> None:
    policy = SkillLoadingPolicy(skills_dir=tmp_path)
    result = policy.select_for_agent_launch(
        cwd=tmp_path,
        overlay_skill_metadata={},
        task="",
        ticket_status="",
        explicit_phase="coding",
        explicit_skills=[],
        overlay_active=False,
    )
    assert result.lifecycle_skill == "t3-code"
    assert "t3-code" in result.skills


def test_agent_launch_status_without_lifecycle_asks_user(tmp_path: Path) -> None:
    """An unrecognized ticket_status that maps to no lifecycle still asks user."""
    policy = SkillLoadingPolicy(skills_dir=tmp_path)
    result = policy.select_for_agent_launch(
        cwd=tmp_path,
        overlay_skill_metadata={},
        task="",
        ticket_status="unknown-status",
        explicit_phase="",
        explicit_skills=[],
        overlay_active=False,
    )
    assert result.ask_user is True


def test_prompt_hook_with_supplementary_skills(tmp_path: Path) -> None:
    policy = SkillLoadingPolicy(skills_dir=tmp_path)
    result = policy.select_for_prompt_hook(
        cwd=tmp_path,
        intent="t3-code",
        overlay_skill_metadata={},
        loaded_skills=set(),
        supplementary_skills=["custom-skill"],
    )
    assert "custom-skill" in result.skills
    assert "t3-code" in result.skills


def test_prompt_hook_no_intent(tmp_path: Path) -> None:
    policy = SkillLoadingPolicy(skills_dir=tmp_path)
    result = policy.select_for_prompt_hook(
        cwd=tmp_path,
        intent="",
        overlay_skill_metadata={},
        loaded_skills=set(),
    )
    assert result.lifecycle_skill == ""


def test_lifecycle_for_task_no_match() -> None:
    assert SkillLoadingPolicy.lifecycle_for_task_text("hello world") == ""


def test_overlay_skill_no_lifecycle_returns_empty(tmp_path: Path) -> None:
    """overlay_skill_for_context returns empty when no lifecycle and not overlay_active."""
    policy = SkillLoadingPolicy(skills_dir=tmp_path)
    result = policy.select_for_prompt_hook(
        cwd=tmp_path,
        intent="",
        overlay_skill_metadata={"skill_path": "t3-acme", "remote_patterns": ["*"]},
        loaded_skills=set(),
    )
    assert "t3-acme" not in result.skills


def test_overlay_skill_non_list_patterns(tmp_path: Path) -> None:
    """Non-list remote_patterns is treated as no patterns."""
    policy = SkillLoadingPolicy(skills_dir=tmp_path)
    result = policy.select_for_prompt_hook(
        cwd=tmp_path,
        intent="t3-code",
        overlay_skill_metadata={"skill_path": "t3-acme", "remote_patterns": "not-a-list"},
        loaded_skills=set(),
    )
    assert "t3-acme" not in result.skills


def test_overlay_skill_empty_patterns(tmp_path: Path) -> None:
    """Empty remote_patterns list means no overlay skill."""
    policy = SkillLoadingPolicy(skills_dir=tmp_path)
    result = policy.select_for_prompt_hook(
        cwd=tmp_path,
        intent="t3-code",
        overlay_skill_metadata={"skill_path": "t3-acme", "remote_patterns": []},
        loaded_skills=set(),
    )
    assert "t3-acme" not in result.skills


def test_detect_framework_python_from_setup_py(tmp_path: Path) -> None:
    (tmp_path / "setup.py").write_text("from setuptools import setup\n")
    assert SkillLoadingPolicy.detect_framework_skills(tmp_path) == ["ac-python"]


def test_detect_framework_python_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "foo"\n')
    assert SkillLoadingPolicy.detect_framework_skills(tmp_path) == ["ac-python"]


def test_detect_framework_django_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('dependencies = ["django>=5"]\n')
    assert SkillLoadingPolicy.detect_framework_skills(tmp_path) == ["ac-django"]


def test_detect_framework_pyproject_read_error(tmp_path: Path) -> None:
    pp = tmp_path / "pyproject.toml"
    pp.write_text("placeholder", encoding="utf-8")
    pp.chmod(0o000)  # unreadable — read_text raises PermissionError (an OSError)
    try:
        assert SkillLoadingPolicy.detect_framework_skills(tmp_path) == []
    finally:
        pp.chmod(0o644)  # restore for cleanup


def test_detect_framework_nothing(tmp_path: Path) -> None:
    assert SkillLoadingPolicy.detect_framework_skills(tmp_path) == []


def test_resolve_and_dedupe_filters_adopting_ruff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "teetree.skill_loading.resolve_dependencies",
        lambda skills, **_kw: ["t3-code", "ac-adopting-ruff", "t3-debug"],
    )
    policy = SkillLoadingPolicy(skills_dir=tmp_path)
    result = policy._resolve_and_dedupe(["t3-code"])
    assert "ac-adopting-ruff" not in result


def test_git_remote_urls_fallback_to_verbose(tmp_path: Path) -> None:
    """When origin has no URL, fall back to git remote -v."""
    mock_origin_fail = MagicMock(returncode=1, stdout="")
    mock_verbose_ok = MagicMock(
        returncode=0, stdout="upstream\tgit@gitlab.com:foo/bar (fetch)\nupstream\tgit@gitlab.com:foo/bar (push)\n"
    )

    with patch("teetree.skill_loading.subprocess.run", side_effect=[mock_origin_fail, mock_verbose_ok]):
        urls = SkillLoadingPolicy._git_remote_urls(tmp_path)

    assert urls == ["git@gitlab.com:foo/bar"]


def test_git_remote_urls_all_fail(tmp_path: Path) -> None:
    mock_fail = MagicMock(returncode=1, stdout="")
    with patch("teetree.skill_loading.subprocess.run", return_value=mock_fail):
        urls = SkillLoadingPolicy._git_remote_urls(tmp_path)
    assert urls == []


def test_git_remote_url_os_error(tmp_path: Path) -> None:
    with patch("teetree.skill_loading.subprocess.run", side_effect=OSError("no git")):
        assert SkillLoadingPolicy._git_remote_url(tmp_path, "origin") == ""


def test_git_remote_urls_verbose_os_error(tmp_path: Path) -> None:
    mock_origin_fail = MagicMock(returncode=1, stdout="")
    with patch("teetree.skill_loading.subprocess.run", side_effect=[mock_origin_fail, OSError("no git")]):
        urls = SkillLoadingPolicy._git_remote_urls(tmp_path)
    assert urls == []
