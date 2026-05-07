"""Integration tests for ``teatree.core.skill_cache``."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import teatree.core.skill_cache as skill_cache_mod
from teatree.core.skill_cache import (
    _build_trigger_index,
    _collect_skill_mtimes,
    _validate_skills,
    write_skill_metadata_cache,
)

_VALID_FRONTMATTER = (
    "---\n"
    "name: example\n"
    "description: example skill\n"
    "triggers:\n"
    "    priority: 50\n"
    "    keywords:\n"
    "        - foo\n"
    "        - bar\n"
    "---\n"
    "\n"
    "# Body\n"
)


def _make_skills_dir(tmp_path: Path, skills: dict[str, str | None]) -> Path:
    """Build a `~/.claude/skills`-shaped directory under tmp_path.

    Each entry maps a skill folder name to the SKILL.md content (or None
    for "no SKILL.md, gets skipped").
    """
    root = tmp_path / "skills"
    root.mkdir()
    for name, content in skills.items():
        d = root / name
        d.mkdir()
        if content is not None:
            (d / "SKILL.md").write_text(content, encoding="utf-8")
    return root


class TestBuildTriggerIndex:
    def test_returns_empty_when_skills_dir_missing(self, tmp_path: Path) -> None:
        with patch.object(skill_cache_mod, "_CLAUDE_SKILLS_DIR", tmp_path / "missing"):
            assert _build_trigger_index() == []

    def test_extracts_triggers_from_skill_md(self, tmp_path: Path) -> None:
        skills_dir = _make_skills_dir(
            tmp_path,
            {
                "example": _VALID_FRONTMATTER,
                "no-skill-md": None,
            },
        )
        with patch.object(skill_cache_mod, "_CLAUDE_SKILLS_DIR", skills_dir):
            index = _build_trigger_index()

        assert len(index) == 1
        assert index[0]["skill"] == "example"
        assert index[0]["priority"] == 50

    def test_skips_skill_without_frontmatter_triggers(self, tmp_path: Path) -> None:
        skills_dir = _make_skills_dir(tmp_path, {"plain": "# No frontmatter"})
        with patch.object(skill_cache_mod, "_CLAUDE_SKILLS_DIR", skills_dir):
            assert _build_trigger_index() == []

    def test_sorts_by_priority_then_skill(self, tmp_path: Path) -> None:
        low_priority = _VALID_FRONTMATTER.replace("priority: 50", "priority: 10")
        skills_dir = _make_skills_dir(
            tmp_path,
            {
                "high": _VALID_FRONTMATTER,
                "low": low_priority,
            },
        )
        with patch.object(skill_cache_mod, "_CLAUDE_SKILLS_DIR", skills_dir):
            index = _build_trigger_index()

        assert [entry["skill"] for entry in index] == ["low", "high"]


class TestCollectSkillMtimes:
    def test_returns_empty_when_skills_dir_missing(self, tmp_path: Path) -> None:
        with patch.object(skill_cache_mod, "_CLAUDE_SKILLS_DIR", tmp_path / "missing"):
            assert _collect_skill_mtimes() == {}

    def test_collects_mtime_ns_per_skill(self, tmp_path: Path) -> None:
        skills_dir = _make_skills_dir(
            tmp_path,
            {
                "example": "# Example",
                "no-skill-md": None,
            },
        )
        with patch.object(skill_cache_mod, "_CLAUDE_SKILLS_DIR", skills_dir):
            mtimes = _collect_skill_mtimes()

        assert "example" in mtimes
        assert "no-skill-md" not in mtimes
        assert isinstance(mtimes["example"], int)


class TestValidateSkills:
    def test_no_op_when_skills_dir_missing(self, tmp_path: Path) -> None:
        with patch.object(skill_cache_mod, "_CLAUDE_SKILLS_DIR", tmp_path / "missing"):
            _validate_skills(set())

    def test_iterates_over_skill_md_files(self, tmp_path: Path) -> None:
        skills_dir = _make_skills_dir(
            tmp_path,
            {
                "example": _VALID_FRONTMATTER,
                "no-skill-md": None,
            },
        )
        with (
            patch.object(skill_cache_mod, "_CLAUDE_SKILLS_DIR", skills_dir),
            patch.object(skill_cache_mod, "validate_skill_md", return_value=([], [])) as validator,
        ):
            _validate_skills({"example"})
        validator.assert_called_once()

    def test_logs_errors_and_warnings(self, tmp_path: Path) -> None:
        skills_dir = _make_skills_dir(tmp_path, {"example": _VALID_FRONTMATTER})
        with (
            patch.object(skill_cache_mod, "_CLAUDE_SKILLS_DIR", skills_dir),
            patch.object(skill_cache_mod, "validate_skill_md", return_value=(["err"], ["warn"])),
            patch.object(skill_cache_mod.logger, "warning") as log_warning,
        ):
            _validate_skills(set())
        assert log_warning.call_count >= 2


class TestWriteSkillMetadataCache:
    def test_writes_json_with_trigger_index(self, tmp_path: Path) -> None:
        skills_dir = _make_skills_dir(tmp_path, {"example": _VALID_FRONTMATTER})
        overlay = MagicMock()
        overlay.metadata.get_skill_metadata.return_value = {"skill_path": "skills/foo"}
        data_dir = tmp_path / "data"
        with (
            patch.object(skill_cache_mod, "_CLAUDE_SKILLS_DIR", skills_dir),
            patch.object(skill_cache_mod, "DATA_DIR", data_dir),
            patch.object(skill_cache_mod, "get_overlay", return_value=overlay),
            patch.object(skill_cache_mod, "resolve_all", return_value={}),
        ):
            write_skill_metadata_cache()

        cache_file = data_dir / "skill-metadata.json"
        assert cache_file.is_file()
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        assert payload["skill_path"] == "skills/foo"
        assert payload["trigger_index"][0]["skill"] == "example"
        assert "teatree_version" in payload
        assert "skill_mtimes" in payload
