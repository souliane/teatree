"""Tests for scripts/lib/skill_loader.py.

Skill suggestion is cwd/overlay-context only — framework skills detected from
the prompt's cwd plus advisory supplementary skills. There is no free-text
scan of the prompt; the lifecycle skill loads explicitly via slash command /
phase / requires-chain elsewhere.
"""

from __future__ import annotations  # noqa: TID251 — test for standalone script

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# skill_loader lives in scripts/lib/, add scripts/ to path
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import skill_loader as skill_loader_mod  # noqa: E402
from lib.skill_loader import build_requires_index, read_supplementary_skills, suggest_skills  # noqa: E402

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


# ── Build requires index from skills directory ───────────────────────


class TestBuildRequiresIndex:
    def test_builds_from_skills_dir(self):
        if not SKILLS_DIR.is_dir():
            pytest.skip("skills directory not found")
        index = build_requires_index([SKILLS_DIR])
        by_skill = {e["skill"]: e for e in index}
        assert "code" in by_skill
        # ``code`` requires the migrated companion + its declared deps.
        assert "workspace" in by_skill["code"]["requires"]
        assert "test-driven-development" in by_skill["code"]["requires"]
        # Every entry has exactly the three keys.
        assert all(set(e) == {"skill", "requires", "companions"} for e in index)

    def test_sorted_by_skill_name(self):
        if not SKILLS_DIR.is_dir():
            pytest.skip("skills directory not found")
        index = build_requires_index([SKILLS_DIR])
        names = [e["skill"] for e in index]
        assert names == sorted(names)

    def test_empty_dir(self, tmp_path):
        assert build_requires_index([tmp_path]) == []

    def test_skill_without_requires_gets_empty_list(self, tmp_path):
        skill_dir = tmp_path / "no-requires"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: no-requires\n---\n# No requires")
        assert build_requires_index([tmp_path]) == [{"skill": "no-requires", "requires": [], "companions": []}]

    def test_dedup_across_search_dirs(self, tmp_path):
        first = tmp_path / "a"
        second = tmp_path / "b"
        for root in (first, second):
            skill = root / "code"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: code\nrequires:\n  - workspace\n---\n")
        index = build_requires_index([first, second])
        assert [e["skill"] for e in index] == ["code"]


# ── Cache version validation ─────────────────────────────────────────


class TestMetadataCacheInvalidation:
    def test_valid_version_returns_data(self, tmp_path):
        cache = tmp_path / "skill-metadata.json"
        cache.write_text(json.dumps({"teatree_version": "1.0.0", "skill_index": [{"skill": "test"}]}))
        with (
            mock.patch.object(skill_loader_mod, "SKILL_METADATA_CACHE", cache),
            mock.patch.object(skill_loader_mod, "_get_installed_version", return_value="1.0.0"),
        ):
            result = skill_loader_mod._read_metadata_cache()
            assert result["skill_index"] == [{"skill": "test"}]

    def test_mismatched_version_returns_empty(self, tmp_path):
        cache = tmp_path / "skill-metadata.json"
        cache.write_text(json.dumps({"teatree_version": "1.0.0", "skill_index": [{"skill": "test"}]}))
        with (
            mock.patch.object(skill_loader_mod, "SKILL_METADATA_CACHE", cache),
            mock.patch.object(skill_loader_mod, "_get_installed_version", return_value="2.0.0"),
        ):
            assert skill_loader_mod._read_metadata_cache() == {}

    def test_missing_version_in_cache_skips_check(self, tmp_path):
        cache = tmp_path / "skill-metadata.json"
        cache.write_text(json.dumps({"skill_index": [{"skill": "test"}]}))
        with mock.patch.object(skill_loader_mod, "SKILL_METADATA_CACHE", cache):
            result = skill_loader_mod._read_metadata_cache()
            assert result["skill_index"] == [{"skill": "test"}]


class TestSupplementarySkills:
    def test_reads_config(self, tmp_path):
        config = tmp_path / "skills.yml"
        config.write_text("ac-django: '.'\nac-ruff: '\\b(ruff)\\b'\n")
        assert read_supplementary_skills(str(config), "hello") == ["ac-django"]
        assert read_supplementary_skills(str(config), "adopt ruff") == ["ac-django", "ac-ruff"]

    def test_missing_config(self):
        assert read_supplementary_skills("/nonexistent", "hello") == []

    def test_comments_and_blanks(self, tmp_path):
        config = tmp_path / "skills.yml"
        config.write_text("# comment\n\nac-django: '.'\n")
        assert read_supplementary_skills(str(config), "hello") == ["ac-django"]


class TestSuggestSkills:
    """cwd-based framework detection, no prompt scan."""

    def test_django_cwd_surfaces_framework_skill(self, tmp_path):
        (tmp_path / "manage.py").write_text("# django project\n", encoding="utf-8")
        result = suggest_skills(
            {
                "prompt": "anything at all",
                "cwd": str(tmp_path),
                "loaded_skills": [],
                "supplementary_config": "",
            }
        )
        assert "ac-django" in result["suggestions"]

    def test_filters_loaded(self, tmp_path):
        (tmp_path / "manage.py").write_text("# django project\n", encoding="utf-8")
        result = suggest_skills(
            {
                "prompt": "anything",
                "cwd": str(tmp_path),
                "loaded_skills": ["ac-django"],
                "supplementary_config": "",
            }
        )
        assert "ac-django" not in result["suggestions"]

    def test_non_python_cwd_surfaces_nothing(self, tmp_path):
        result = suggest_skills(
            {
                "prompt": "hello",
                "cwd": str(tmp_path),
                "loaded_skills": [],
                "supplementary_config": "",
            }
        )
        assert result["suggestions"] == []

    def test_supplementary_is_advisory(self, tmp_path):
        config = tmp_path / "skills.yml"
        config.write_text("ac-ruff: '\\bruff\\b'\n")
        result = suggest_skills(
            {
                "prompt": "run ruff check",
                "cwd": str(tmp_path),
                "loaded_skills": [],
                "supplementary_config": str(config),
            }
        )
        assert "ac-ruff" in result["suggestions"]
        assert "ac-ruff" in result["advisory"]
