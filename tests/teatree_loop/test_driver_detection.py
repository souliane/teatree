"""``detect_driver`` — resolve the tick driver at claim time (PR-26 / M9).

Worker = ``loop_runner_enabled`` ON AND a live worker holding the kernel flock;
self-pump = the loop-registry ``t3-loop-tick-owner`` record naming this session
with a live pid; anything else = driverless (``""``). ``external`` is never
auto-detected. The substrate-agnostic pin flips ``loop_runner_enabled`` around
the SAME detection call and asserts the output tracks it — the proof the
detection survives the loop-runner default flip.
"""

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from teatree.loop.driver_detection import detect_driver
from teatree.utils import singleton as singleton_mod
from teatree.utils.singleton import WORKER_SINGLETON, singleton

_OWNER_KEY = "t3-loop-tick-owner"  # gitleaks:allow — registry slot name, not a credential


def _set_loop_runner(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    # Patch where it is looked up (driver_detection binds it at import), not its source.
    monkeypatch.setattr(
        "teatree.loop.driver_detection.get_effective_settings",
        lambda *a, **k: types.SimpleNamespace(loop_runner_enabled=enabled),
    )


def _write_owner_record(registry_dir: Path, *, session_id: str, pid: int) -> None:
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / "loop-registry.json").write_text(
        json.dumps({_OWNER_KEY: {"session_id": session_id, "pid": pid}}), encoding="utf-8"
    )


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class TestWorkerDetection:
    def test_flag_on_and_held_flock_is_loop_runner(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_loop_runner(monkeypatch, enabled=True)
        monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path)
        with singleton(WORKER_SINGLETON):
            assert detect_driver("sess-a") == "loop_runner"

    def test_flag_on_but_free_flock_is_not_loop_runner(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The "worker enabled but not running" hole — the DRIVERLESS case, not loop_runner.
        _set_loop_runner(monkeypatch, enabled=True)
        monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        assert detect_driver("sess-a") == ""

    def test_flag_off_with_held_flock_is_not_loop_runner(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_loop_runner(monkeypatch, enabled=False)
        monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        with singleton(WORKER_SINGLETON):
            assert detect_driver("sess-a") == ""


class TestSelfPumpDetection:
    def test_registry_record_for_this_session_with_live_pid_is_self_pump(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_loop_runner(monkeypatch, enabled=False)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        _write_owner_record(tmp_path, session_id="sess-a", pid=os.getpid())
        assert detect_driver("sess-a") == "self_pump"

    def test_registry_record_for_different_session_is_not_self_pump(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_loop_runner(monkeypatch, enabled=False)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        _write_owner_record(tmp_path, session_id="other", pid=os.getpid())
        assert detect_driver("sess-a") == ""

    def test_registry_record_with_dead_pid_is_not_self_pump(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_loop_runner(monkeypatch, enabled=False)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        _write_owner_record(tmp_path, session_id="sess-a", pid=_dead_pid())
        assert detect_driver("sess-a") == ""

    def test_empty_session_is_never_self_pump(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_loop_runner(monkeypatch, enabled=False)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        _write_owner_record(tmp_path, session_id="", pid=os.getpid())
        assert detect_driver("") == ""


class TestDriverless:
    def test_no_worker_no_registry_is_driverless(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_loop_runner(monkeypatch, enabled=False)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        assert detect_driver("sess-a") == ""


class TestSubstrateAgnostic:
    def test_detection_tracks_the_loop_runner_flag_around_the_same_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same held flock, same session, same call path — only the live
        # loop_runner_enabled value differs, and detection tracks it. No branch
        # references any cron plane; this is the survives-the-scheduling-flip proof.
        monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        with singleton(WORKER_SINGLETON):
            _set_loop_runner(monkeypatch, enabled=False)
            assert detect_driver("sess-a") == ""
            _set_loop_runner(monkeypatch, enabled=True)
            assert detect_driver("sess-a") == "loop_runner"
