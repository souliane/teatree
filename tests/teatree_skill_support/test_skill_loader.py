"""Tests for scripts/lib/skill_loader.py.

Skill suggestion is cwd/overlay-context only — framework skills detected from the
session's cwd. There is no free-text scan of any prompt; the lifecycle skill loads
explicitly via slash command / phase / requires-chain elsewhere.
"""

from __future__ import annotations  # noqa: TID251 — test for standalone script

import json
import sys
from importlib import import_module
from pathlib import Path
from unittest import mock

# skill_loader lives in scripts/lib/, add scripts/ to path
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

skill_loader_mod = import_module("lib.skill_loader")
suggest_skills = skill_loader_mod.suggest_skills

# ── Cache version validation ─────────────────────────────────────────


class TestMetadataCacheInvalidation:
    @staticmethod
    def _seed_cache(tmp_path, monkeypatch, payload: dict) -> None:
        """Write *payload* where the reader's XDG resolution will look for it."""
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        cache = skill_loader_mod.skill_metadata_cache()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload))

    def test_valid_version_returns_data(self, tmp_path, monkeypatch):
        self._seed_cache(tmp_path, monkeypatch, {"teatree_version": "1.0.0", "skill_index": [{"skill": "test"}]})
        with mock.patch.object(skill_loader_mod, "_get_installed_version", return_value="1.0.0"):
            result = skill_loader_mod._read_metadata_cache()
            assert result["skill_index"] == [{"skill": "test"}]

    def test_mismatched_version_returns_empty(self, tmp_path, monkeypatch):
        self._seed_cache(tmp_path, monkeypatch, {"teatree_version": "1.0.0", "skill_index": [{"skill": "test"}]})
        with mock.patch.object(skill_loader_mod, "_get_installed_version", return_value="2.0.0"):
            assert skill_loader_mod._read_metadata_cache() == {}

    def test_no_cache_hands_the_policy_none_so_it_builds_the_live_index(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        assert skill_loader_mod._read_skill_index() is None

    def test_a_skill_added_to_any_root_makes_the_cache_stale(self, tmp_path, monkeypatch):
        installed = Path.home() / ".agents" / "skills" / "late"
        fingerprint = skill_loader_mod.skill_mtimes(skill_loader_mod.harness_skills_dirs())
        assert skill_loader_mod._cache_is_stale({"skill_mtimes": fingerprint}) is False
        installed.mkdir(parents=True)
        (installed / "SKILL.md").write_text("---\nname: late\n---\n", encoding="utf-8")
        assert skill_loader_mod._cache_is_stale({"skill_mtimes": fingerprint}) is True

    def test_missing_version_in_cache_skips_check(self, tmp_path, monkeypatch):
        self._seed_cache(tmp_path, monkeypatch, {"skill_index": [{"skill": "test"}]})
        result = skill_loader_mod._read_metadata_cache()
        assert result["skill_index"] == [{"skill": "test"}]


class TestSuggestSkills:
    """cwd-based framework detection, no prompt scan."""

    def test_django_cwd_surfaces_framework_skill(self, tmp_path):
        (tmp_path / "manage.py").write_text("# django project\n", encoding="utf-8")
        result = suggest_skills(
            {
                "cwd": str(tmp_path),
                "loaded_skills": [],
            }
        )
        assert "ac-django" in result["suggestions"]

    def test_filters_loaded(self, tmp_path):
        (tmp_path / "manage.py").write_text("# django project\n", encoding="utf-8")
        result = suggest_skills(
            {
                "cwd": str(tmp_path),
                "loaded_skills": ["ac-django"],
            }
        )
        assert "ac-django" not in result["suggestions"]

    def test_non_python_cwd_surfaces_nothing(self, tmp_path):
        result = suggest_skills(
            {
                "cwd": str(tmp_path),
                "loaded_skills": [],
            }
        )
        assert result["suggestions"] == []
