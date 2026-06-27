"""Tests for post-compaction snapshot recovery (issue #845).

The harness fires ``PostCompact``, but per the Claude Code hook response
schema (``docs/claude-code-internals.md`` §3, sourced from
``claurst/spec/12_constants_types.md`` § 24.4) ``PostCompact`` has **no**
``hookSpecificOutput`` entry — it cannot inject ``additionalContext``. The
only post-compaction event whose output the harness reads is
``SessionStart`` with ``source == "compact"``. Recovery therefore happens
in the SessionStart bootstrap handler; the snapshot itself is written,
agent-action-free, by the ``PreCompact`` handler. These tests drive the
realistic event sequence (PreCompact write -> SessionStart/compact
recover) rather than calling a handler whose output the harness discards.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    _T3_TEMP_PREFIX,
    _find_temp_files,
    _recover_snapshot_context,
    handle_pre_compact,
    handle_session_start_bootstrap,
)


@pytest.fixture(autouse=True)
def _isolate_filesystem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point STATE_DIR/_TMP_DIR and the loop registry at temp dirs."""
    original_state, original_tmp = router.STATE_DIR, router._TMP_DIR
    router.STATE_DIR = tmp_path / "state"
    router._TMP_DIR = tmp_path / "tmp"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    router._TMP_DIR.mkdir(parents=True, exist_ok=True)
    reg_dir = tmp_path / "data"
    reg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setattr(router, "_TTY_PATH", str(tmp_path / "fake-tty"))
    # Force the teatree opt-in marker AND the #256 auto-load opt-in active:
    # these cover the post-compaction snapshot-recovery mechanism, not the
    # opt-in gates.
    monkeypatch.setattr(router, "_teatree_active", lambda session_id: True)
    monkeypatch.setattr(router, "_autoload_enabled", lambda: True)
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


class TestRecoverSnapshotContext:
    """Direct coverage of the recovery-context builder's edge branches."""

    def test_no_files_returns_none(self) -> None:
        assert _recover_snapshot_context("no-such-session") is None

    def test_unreadable_file_is_skipped(self) -> None:
        session_id = "perm-denied"
        f = router.STATE_DIR / f"{_T3_TEMP_PREFIX}{session_id}-20260403-1200.md"
        f.write_text("unreadable", encoding="utf-8")
        f.chmod(0o000)
        try:
            # The only snapshot is unreadable -> nothing recoverable.
            assert _recover_snapshot_context(session_id) is None
        finally:
            f.chmod(0o644)


class TestPreCompactWritesSnapshotWithoutAgentAction:
    """The fail-safe: PreCompact persists durable state with zero agent action."""

    def test_precompact_writes_snapshot_file_from_durable_state(self) -> None:
        session_id = "auto-sess"
        (router.STATE_DIR / f"{session_id}.active").write_text("/repo/a\n", encoding="utf-8")

        handle_pre_compact({"session_id": session_id})

        snap = router.STATE_DIR / f"{_T3_TEMP_PREFIX}{session_id}-precompact.md"
        assert snap.is_file()
        assert "/repo/a" in snap.read_text(encoding="utf-8")


class TestSessionStartCompactRecoversSnapshot:
    """Recovery on the only post-compaction event the harness reads."""

    def test_source_compact_reinjects_precompact_snapshot(self, capsys: pytest.CaptureFixture[str]) -> None:
        session_id = "round-trip-sess"
        (router.STATE_DIR / f"{session_id}.active").write_text("/repo/work\n", encoding="utf-8")

        handle_pre_compact({"session_id": session_id})
        capsys.readouterr()  # discard PreCompact's own stdout

        handle_session_start_bootstrap({"session_id": session_id, "source": "compact"})

        out = capsys.readouterr().out
        # Exactly one JSON object on stdout (chained writes would be invalid).
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "PRE-COMPACTION SNAPSHOTS RECOVERED" in ctx
        assert "/repo/work" in ctx
        # The tick-dispatch directive is preserved in the same payload.
        assert "t3 loop tick" in ctx

    def test_recovers_arbitrary_snapshot_content(self, capsys: pytest.CaptureFixture[str]) -> None:
        session_id = "sess-456"
        f = router.STATE_DIR / f"{_T3_TEMP_PREFIX}{session_id}-20260403-1200.md"
        f.write_text("# Retro Findings\n\n- Learned X\n- Fixed Y", encoding="utf-8")

        handle_session_start_bootstrap({"session_id": session_id, "source": "compact"})

        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert "Learned X" in ctx
        assert "Fixed Y" in ctx
        assert "PRE-COMPACTION SNAPSHOTS RECOVERED" in ctx

    def test_multiple_snapshots_are_joined(self, capsys: pytest.CaptureFixture[str]) -> None:
        session_id = "sess-multi"
        for i, content in enumerate(["Finding A", "Finding B"]):
            f = router.STATE_DIR / f"{_T3_TEMP_PREFIX}{session_id}-20260403-120{i}.md"
            f.write_text(content, encoding="utf-8")

        handle_session_start_bootstrap({"session_id": session_id, "source": "compact"})

        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert "Finding A" in ctx
        assert "Finding B" in ctx

    def test_empty_snapshot_is_not_injected(self, capsys: pytest.CaptureFixture[str]) -> None:
        session_id = "sess-empty"
        f = router.STATE_DIR / f"{_T3_TEMP_PREFIX}{session_id}-20260403-1200.md"
        f.write_text("", encoding="utf-8")

        handle_session_start_bootstrap({"session_id": session_id, "source": "compact"})

        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert "PRE-COMPACTION SNAPSHOTS RECOVERED" not in ctx

    def test_non_compact_source_does_not_inject_recovery(self, capsys: pytest.CaptureFixture[str]) -> None:
        session_id = "sess-startup"
        f = router.STATE_DIR / f"{_T3_TEMP_PREFIX}{session_id}-20260403-1200.md"
        f.write_text("stale snapshot", encoding="utf-8")

        handle_session_start_bootstrap({"session_id": session_id, "source": "startup"})

        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert "PRE-COMPACTION SNAPSHOTS RECOVERED" not in ctx
        assert "stale snapshot" not in ctx

    def test_missing_source_field_defaults_to_no_recovery(self, capsys: pytest.CaptureFixture[str]) -> None:
        session_id = "sess-nosrc"
        f = router.STATE_DIR / f"{_T3_TEMP_PREFIX}{session_id}-20260403-1200.md"
        f.write_text("stale snapshot", encoding="utf-8")

        handle_session_start_bootstrap({"session_id": session_id})

        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert "PRE-COMPACTION SNAPSHOTS RECOVERED" not in ctx


class TestPostCompactHookIsNotRegistered:
    """The harness discards PostCompact output (no hookSpecificOutput entry)."""

    def test_postcompact_not_in_handler_table(self) -> None:
        assert "PostCompact" not in router._HANDLERS

    def test_no_handle_post_compact_symbol(self) -> None:
        assert not hasattr(router, "handle_post_compact")
