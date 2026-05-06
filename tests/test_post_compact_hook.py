"""Tests for the PostCompact hook handler (pre-compaction snapshot recovery)."""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _T3_TEMP_PREFIX, _find_temp_files, handle_post_compact


@pytest.fixture(autouse=True)
def _isolate_filesystem(tmp_path: Path):
    """Point STATE_DIR and _TMP_DIR at temp directories so tests don't see real /tmp."""
    original_state, original_tmp = router.STATE_DIR, router._TMP_DIR
    router.STATE_DIR = tmp_path / "state"
    router._TMP_DIR = tmp_path / "tmp"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    router._TMP_DIR.mkdir(parents=True, exist_ok=True)
    yield
    router.STATE_DIR = original_state
    router._TMP_DIR = original_tmp


class TestFindTempFiles:
    def test_no_files_returns_empty(self) -> None:
        assert _find_temp_files("sess-123") == []

    def test_finds_session_specific_file_in_state_dir(self) -> None:
        f = router.STATE_DIR / f"{_T3_TEMP_PREFIX}sess-123-20260403-1200.md"
        f.write_text("findings", encoding="utf-8")

        result = _find_temp_files("sess-123")
        assert len(result) == 1
        assert result[0].name == f.name

    def test_finds_legacy_files_in_tmp(self) -> None:
        f = router._TMP_DIR / f"{_T3_TEMP_PREFIX}other-session-20260403-1200.md"
        f.write_text("old findings", encoding="utf-8")

        result = _find_temp_files("sess-123")
        assert len(result) == 1
        assert result[0].name == f.name


class TestHandlePostCompact:
    def test_no_files_produces_no_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_post_compact({"session_id": "no-such-session"})
        assert capsys.readouterr().out == ""

    def test_injects_file_content_as_additional_context(self, capsys: pytest.CaptureFixture[str]) -> None:
        session_id = "sess-456"
        f = router.STATE_DIR / f"{_T3_TEMP_PREFIX}{session_id}-20260403-1200.md"
        f.write_text("# Retro Findings\n\n- Learned X\n- Fixed Y", encoding="utf-8")

        handle_post_compact({"session_id": session_id})

        output = json.loads(capsys.readouterr().out)
        assert "additionalContext" in output
        assert "Learned X" in output["additionalContext"]
        assert "Fixed Y" in output["additionalContext"]
        assert "PRE-COMPACTION SNAPSHOTS RECOVERED" in output["additionalContext"]

    def test_empty_file_produces_no_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        session_id = "sess-789"
        f = router.STATE_DIR / f"{_T3_TEMP_PREFIX}{session_id}-20260403-1200.md"
        f.write_text("", encoding="utf-8")

        handle_post_compact({"session_id": session_id})
        assert capsys.readouterr().out == ""

    def test_multiple_files_are_joined(self, capsys: pytest.CaptureFixture[str]) -> None:
        session_id = "sess-multi"
        for i, content in enumerate(["Finding A", "Finding B"]):
            f = router.STATE_DIR / f"{_T3_TEMP_PREFIX}{session_id}-20260403-120{i}.md"
            f.write_text(content, encoding="utf-8")

        handle_post_compact({"session_id": session_id})

        output = json.loads(capsys.readouterr().out)
        ctx = output["additionalContext"]
        assert "Finding A" in ctx
        assert "Finding B" in ctx
