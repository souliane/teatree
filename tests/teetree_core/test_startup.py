"""Tests for teetree.core.views._startup — perform_sync and _write_skill_metadata_cache."""

import json

import pytest
from django.test import override_settings

from teetree.core.sync import SyncResult


@override_settings(TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay")
def test_perform_sync_calls_sync_and_writes_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
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


@override_settings(TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay")
def test_write_skill_metadata_cache_creates_parent_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """_write_skill_metadata_cache creates parent directories if missing."""
    nested = tmp_path / "deep" / "nested"
    monkeypatch.setattr("teetree.core.views._startup.DATA_DIR", nested)

    from teetree.core.views._startup import _write_skill_metadata_cache  # noqa: PLC0415

    _write_skill_metadata_cache()

    cache_path = nested / "skill-metadata.json"
    assert cache_path.exists()
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


@override_settings(TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay")
def test_write_skill_metadata_cache_content_matches_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
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


@override_settings(TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay")
def test_trigger_index_built_from_skills(monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
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


@override_settings(TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay")
def test_trigger_index_skips_skills_without_triggers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
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


# ── _build_trigger_index edge cases ──────────────────────────────────


@override_settings(TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay")
def test_trigger_index_skips_non_directory_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """Non-directory entries in the skills dir are skipped."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "not-a-dir.txt").write_text("just a file")
    monkeypatch.setattr("teetree.core.views._startup._CLAUDE_SKILLS_DIR", skills_dir)

    from teetree.core.views._startup import _build_trigger_index  # noqa: PLC0415

    assert _build_trigger_index() == []


@override_settings(TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay")
def test_trigger_index_skips_dir_without_skill_md(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """Directories without SKILL.md are skipped."""
    skills_dir = tmp_path / "skills"
    (skills_dir / "empty-skill").mkdir(parents=True)
    monkeypatch.setattr("teetree.core.views._startup._CLAUDE_SKILLS_DIR", skills_dir)

    from teetree.core.views._startup import _build_trigger_index  # noqa: PLC0415

    assert _build_trigger_index() == []


@override_settings(TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay")
def test_trigger_index_handles_unreadable_skill_md(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
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


@override_settings(TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay")
def test_trigger_index_resolves_symlinks(monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
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


# ── _parse_triggers edge cases ───────────────────────────────────────


@override_settings(TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay")
def test_trigger_index_returns_empty_when_skills_dir_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """When the skills directory does not exist, an empty index is returned."""
    monkeypatch.setattr("teetree.core.views._startup._CLAUDE_SKILLS_DIR", tmp_path / "nonexistent")

    from teetree.core.views._startup import _build_trigger_index  # noqa: PLC0415

    assert _build_trigger_index() == []


def test_parse_triggers_no_frontmatter() -> None:
    from teetree.core.views._startup import _parse_triggers  # noqa: PLC0415

    assert _parse_triggers("# No frontmatter") is None


def test_parse_triggers_no_closing_fence() -> None:
    from teetree.core.views._startup import _parse_triggers  # noqa: PLC0415

    assert _parse_triggers("---\nname: test\ntriggers:\n  keywords:\n") is None


def test_parse_triggers_all_fields() -> None:
    from teetree.core.views._startup import _parse_triggers  # noqa: PLC0415

    md = (
        "---\nname: test\ntriggers:\n  priority: 5\n  exclude: '\\bno\\b'\n"
        "  end_of_session: true\n  keywords:\n    - '\\bfoo\\b'\n"
        "  urls:\n    - 'https://example.com'\nmetadata:\n  version: 1\n---\n"
    )
    result = _parse_triggers(md)
    assert result is not None
    assert result["priority"] == 5
    assert result["exclude"] == r"\bno\b"
    assert result["end_of_session"] is True
    assert result["keywords"] == [r"\bfoo\b"]
    assert result["urls"] == ["https://example.com"]


def test_parse_triggers_terminated_by_next_top_level_key() -> None:
    from teetree.core.views._startup import _parse_triggers  # noqa: PLC0415

    md = "---\nname: test\ntriggers:\n  keywords:\n    - '\\bbar\\b'\nrequires:\n  - dep\n---\n"
    result = _parse_triggers(md)
    assert result is not None
    assert result["keywords"] == [r"\bbar\b"]


# ── _parse_trigger_line coverage ─────────────────────────────────────


def test_parse_trigger_line_all_branches() -> None:
    from teetree.core.views._startup import _parse_trigger_line  # noqa: PLC0415

    triggers: dict = {"priority": 50, "keywords": [], "urls": [], "exclude": "", "end_of_session": False}

    assert _parse_trigger_line("priority: 10", triggers, "") == ""
    assert triggers["priority"] == 10

    assert _parse_trigger_line("exclude: '\\bx\\b'", triggers, "") == ""
    assert triggers["exclude"] == r"\bx\b"

    assert _parse_trigger_line("end_of_session: true", triggers, "") == ""
    assert triggers["end_of_session"] is True

    assert _parse_trigger_line("keywords:", triggers, "") == "keywords"
    assert _parse_trigger_line("urls:", triggers, "") == "urls"

    assert _parse_trigger_line("- '\\bfoo\\b'", triggers, "keywords") == "keywords"
    assert triggers["keywords"] == [r"\bfoo\b"]

    assert _parse_trigger_line("- 'https://x'", triggers, "urls") == "urls"
    assert triggers["urls"] == ["https://x"]

    # Non-matching line returns current_key unchanged
    assert _parse_trigger_line("something_else", triggers, "keywords") == "keywords"
