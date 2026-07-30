# test-path: cross-cutting
"""Stop-event 5-minute snapshot slot + PreCompact adapter (souliane/teatree#2564, PR-20).

The slot is throttled to a 5-minute cadence, runs even while loops are paused
(it never consults availability), is guarded by a kill-switch, and never blocks
the turn. The PreCompact adapter refreshes the snapshot unthrottled.
"""

import os
import time
from pathlib import Path

import pytest

import hooks.scripts.stop_snapshot_slot as slot


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_HOOK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("TEATREE_CLAUDE_STATUSLINE_STATE_DIR", raising=False)


class TestSlotThrottle:
    def test_first_run_is_due_and_calls_prepare_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, dict]] = []
        monkeypatch.setattr(slot, "_slot_enabled", lambda: True)
        monkeypatch.setattr(slot, "_run_prepare_stop", lambda sid, data: calls.append((sid, data)))
        assert slot.handle_stop_snapshot_slot({"session_id": "s1", "cwd": "/x"}) is None
        assert calls == [("s1", {"session_id": "s1", "cwd": "/x"})]
        assert slot._slot_marker("s1").exists()

    def test_throttled_within_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(slot, "_slot_enabled", lambda: True)
        monkeypatch.setattr(slot, "_run_prepare_stop", lambda sid, _d: calls.append(sid))
        slot.handle_stop_snapshot_slot({"session_id": "s1"})
        slot.handle_stop_snapshot_slot({"session_id": "s1"})  # immediately again
        assert calls == ["s1"]  # second call throttled

    def test_due_again_after_interval_elapses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(slot, "_slot_enabled", lambda: True)
        monkeypatch.setattr(slot, "_run_prepare_stop", lambda sid, _d: calls.append(sid))
        slot.handle_stop_snapshot_slot({"session_id": "s1"})
        marker = slot._slot_marker("s1")
        stale = time.time() - (slot._SLOT_INTERVAL_SECONDS + 60)
        os.utime(marker, (stale, stale))
        slot.handle_stop_snapshot_slot({"session_id": "s1"})
        assert calls == ["s1", "s1"]

    def test_artifacts_under_five_minutes_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(slot, "_slot_enabled", lambda: True)
        monkeypatch.setattr(slot, "_run_prepare_stop", lambda _s, _d: None)
        slot.handle_stop_snapshot_slot({"session_id": "s1"})
        age = time.time() - slot._slot_marker("s1").stat().st_mtime
        assert age < slot._SLOT_INTERVAL_SECONDS


class TestSlotIndependentOfAvailability:
    def test_runs_regardless_of_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The slot must fire while loops are paused — it never consults the mode."""
        calls: list[str] = []
        monkeypatch.setattr(slot, "_slot_enabled", lambda: True)
        monkeypatch.setattr(slot, "_run_prepare_stop", lambda sid, _d: calls.append(sid))
        slot.handle_stop_snapshot_slot({"session_id": "s1"})
        assert calls == ["s1"]


class TestKillSwitch:
    def test_disabled_setting_skips_the_slot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree_settings.teatree_bool_setting", lambda *_a, **_k: False)
        calls: list[str] = []
        monkeypatch.setattr(slot, "_run_prepare_stop", lambda sid, _d: calls.append(sid))
        assert slot.handle_stop_snapshot_slot({"session_id": "s1"}) is None
        assert calls == []

    def test_enabled_by_default_on_broken_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_a: object, **_k: object) -> bool:
            raise OSError

        monkeypatch.setattr("teatree_settings.teatree_bool_setting", _boom)
        assert slot._slot_enabled() is True  # fails OPEN to always-on infra


class TestResilience:
    def test_run_prepare_stop_swallows_prepare_stop_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_a: object, **_k: object) -> None:
            raise RuntimeError

        monkeypatch.setattr("teatree.core.stop_snapshot.prepare_stop", _boom)
        # bootstrap succeeds (Django up in pytest); prepare_stop raises → swallowed.
        slot._run_prepare_stop("s1", {"cwd": "/x"})  # must not raise


class TestOpenPrsForRepo:
    def test_non_git_dir_returns_empty(self, tmp_path: Path) -> None:
        assert slot.open_prs_for_repo(tmp_path) == []

    def test_gh_absent_returns_empty(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(slot.subprocess, "check_output", lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError))
        assert slot.open_prs_for_repo(tmp_path) == []

    def test_valid_json_list_returned(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(slot.subprocess, "check_output", lambda *_a, **_k: '[{"number": 5}]')
        assert slot.open_prs_for_repo(tmp_path) == [{"number": 5}]

    def test_malformed_json_returns_empty(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(slot.subprocess, "check_output", lambda *_a, **_k: "not json")
        assert slot.open_prs_for_repo(tmp_path) == []


class TestInternalGuards:
    def test_claim_slot_swallows_oserror(self, tmp_path: Path) -> None:
        (tmp_path / "blocker").write_text("x")  # a FILE where a dir is expected
        slot._claim_slot(tmp_path / "blocker" / "nested.stamp")  # mkdir fails → swallowed, no raise

    def test_run_skips_when_bootstrap_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[object] = []
        monkeypatch.setattr("hooks.scripts.django_bootstrap.bootstrap_teatree_django", lambda: False)
        monkeypatch.setattr("teatree.core.stop_snapshot.prepare_stop", lambda *a, **k: calls.append(a))
        slot._run_prepare_stop("s1", {"cwd": "/x"})
        assert calls == []  # bootstrap failed → prepare_stop never reached


class TestPreCompactAdapter:
    def test_calls_prepare_stop_unthrottled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "teatree.core.stop_snapshot.prepare_stop",
            lambda sid, cwd, **_k: calls.append((sid, cwd)),
        )
        slot.run_prepare_stop_best_effort("s1", {"cwd": "/work"})
        slot.run_prepare_stop_best_effort("s1", {"cwd": "/work"})  # again, no throttle
        assert calls == [("s1", "/work"), ("s1", "/work")]
