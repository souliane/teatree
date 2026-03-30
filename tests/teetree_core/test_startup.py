"""Tests for teetree.core.views._startup — perform_sync and _write_skill_metadata_cache."""

import json

import pytest
from django.test import override_settings

from teetree.core.sync import SyncResult

_OVERLAY = "tests.teetree_core.conftest.CommandOverlay"


class TestPerformSync:
    @override_settings(TEATREE_OVERLAY_CLASS=_OVERLAY)
    def test_calls_sync_and_writes_cache(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
    ) -> None:
        """perform_sync() calls sync_followup and _write_skill_metadata_cache."""
        fake_result = SyncResult(mrs_found=5, tickets_created=2)
        monkeypatch.setattr("teetree.core.views._startup.sync_followup", lambda: fake_result)
        monkeypatch.setattr("teetree.core.views._startup.DATA_DIR", tmp_path)

        from teetree.core.views._startup import perform_sync  # noqa: PLC0415

        result = perform_sync()

        assert result.mrs_found == 5
        assert result.tickets_created == 2

        cache_path = tmp_path / "skill-metadata.json"
        assert cache_path.exists()
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)


class TestWriteSkillMetadataCache:
    @override_settings(TEATREE_OVERLAY_CLASS=_OVERLAY)
    def test_creates_parent_dirs(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
        """_write_skill_metadata_cache creates parent directories if missing."""
        nested = tmp_path / "deep" / "nested"
        monkeypatch.setattr("teetree.core.views._startup.DATA_DIR", nested)

        from teetree.core.views._startup import _write_skill_metadata_cache  # noqa: PLC0415

        _write_skill_metadata_cache()

        cache_path = nested / "skill-metadata.json"
        assert cache_path.exists()
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    @override_settings(TEATREE_OVERLAY_CLASS=_OVERLAY)
    def test_content_matches_overlay(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
        """Cache content matches the overlay's get_skill_metadata() output."""
        monkeypatch.setattr("teetree.core.views._startup.DATA_DIR", tmp_path)

        from teetree.core.views._startup import _write_skill_metadata_cache  # noqa: PLC0415

        _write_skill_metadata_cache()

        cache_path = tmp_path / "skill-metadata.json"
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        # CommandOverlay.get_skill_metadata() returns {} (default from OverlayBase)
        # _write_skill_metadata_cache adds trigger_index from scanning ~/.claude/skills
        assert "trigger_index" in data
        assert isinstance(data["trigger_index"], list)


class TestBuildTriggerIndex:
    @override_settings(TEATREE_OVERLAY_CLASS=_OVERLAY)
    def test_built_from_skills(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
        """Trigger index is built by scanning skill directories for triggers: frontmatter."""
        monkeypatch.setattr("teetree.core.views._startup.DATA_DIR", tmp_path)

        # Create a fake skill with triggers
        skills_dir = tmp_path / "skills"
        skill = skills_dir / "test-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: test-skill\ntriggers:\n  priority: 42\n  keywords:\n    - '\\btest\\b'\n---\n# Test"
        )
        monkeypatch.setattr("teetree.core.views._startup._CLAUDE_SKILLS_DIR", skills_dir)

        from teetree.core.views._startup import _write_skill_metadata_cache  # noqa: PLC0415

        _write_skill_metadata_cache()

        cache_path = tmp_path / "skill-metadata.json"
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        index = data["trigger_index"]
        assert len(index) == 1
        assert index[0]["skill"] == "test-skill"
        assert index[0]["priority"] == 42
        assert index[0]["keywords"] == [r"\btest\b"]

    @override_settings(TEATREE_OVERLAY_CLASS=_OVERLAY)
    def test_skips_skills_without_triggers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
    ) -> None:
        """Skills without triggers: in frontmatter are excluded from the index."""
        monkeypatch.setattr("teetree.core.views._startup.DATA_DIR", tmp_path)

        skills_dir = tmp_path / "skills"
        skill = skills_dir / "no-triggers"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: no-triggers\n---\n# No triggers")
        monkeypatch.setattr("teetree.core.views._startup._CLAUDE_SKILLS_DIR", skills_dir)

        from teetree.core.views._startup import _write_skill_metadata_cache  # noqa: PLC0415

        _write_skill_metadata_cache()

        cache_path = tmp_path / "skill-metadata.json"
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        assert data["trigger_index"] == []

    @override_settings(TEATREE_OVERLAY_CLASS=_OVERLAY)
    def test_skips_non_directory_entries(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
    ) -> None:
        """Non-directory entries in the skills dir are skipped."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "not-a-dir.txt").write_text("just a file")
        monkeypatch.setattr("teetree.core.views._startup._CLAUDE_SKILLS_DIR", skills_dir)

        from teetree.core.views._startup import _build_trigger_index  # noqa: PLC0415

        assert _build_trigger_index() == []

    @override_settings(TEATREE_OVERLAY_CLASS=_OVERLAY)
    def test_skips_dir_without_skill_md(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
    ) -> None:
        """Directories without SKILL.md are skipped."""
        skills_dir = tmp_path / "skills"
        (skills_dir / "empty-skill").mkdir(parents=True)
        monkeypatch.setattr("teetree.core.views._startup._CLAUDE_SKILLS_DIR", skills_dir)

        from teetree.core.views._startup import _build_trigger_index  # noqa: PLC0415

        assert _build_trigger_index() == []

    @override_settings(TEATREE_OVERLAY_CLASS=_OVERLAY)
    def test_handles_unreadable_skill_md(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
    ) -> None:
        """OSError when reading SKILL.md is caught gracefully."""
        skills_dir = tmp_path / "skills"
        skill = skills_dir / "bad-skill"
        skill.mkdir(parents=True)
        skill_md = skill / "SKILL.md"
        skill_md.write_text("---\ntriggers:\n  keywords:\n    - 'x'\n---\n")
        skill_md.chmod(0o000)
        monkeypatch.setattr("teetree.core.views._startup._CLAUDE_SKILLS_DIR", skills_dir)

        from teetree.core.views._startup import _build_trigger_index  # noqa: PLC0415

        result = _build_trigger_index()
        skill_md.chmod(0o644)  # restore for cleanup
        assert result == []

    @override_settings(TEATREE_OVERLAY_CLASS=_OVERLAY)
    def test_resolves_symlinks(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
        """Symlinked skill directories are resolved and their SKILL.md is read."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        real_skill = tmp_path / "real-skill"
        real_skill.mkdir()
        (real_skill / "SKILL.md").write_text(
            "---\nname: linked\ntriggers:\n  priority: 7\n  keywords:\n    - '\\blinked\\b'\n---\n"
        )
        (skills_dir / "linked").symlink_to(real_skill)
        monkeypatch.setattr("teetree.core.views._startup._CLAUDE_SKILLS_DIR", skills_dir)

        from teetree.core.views._startup import _build_trigger_index  # noqa: PLC0415

        index = _build_trigger_index()
        assert len(index) == 1
        assert index[0]["skill"] == "linked"
        assert index[0]["priority"] == 7

    @override_settings(TEATREE_OVERLAY_CLASS=_OVERLAY)
    def test_returns_empty_when_skills_dir_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
    ) -> None:
        """When the skills directory does not exist, an empty index is returned."""
        monkeypatch.setattr("teetree.core.views._startup._CLAUDE_SKILLS_DIR", tmp_path / "nonexistent")

        from teetree.core.views._startup import _build_trigger_index  # noqa: PLC0415

        assert _build_trigger_index() == []


# Parser unit tests live in tests/test_trigger_parser.py (single source of truth).
