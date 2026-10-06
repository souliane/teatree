"""``detect_driver`` — resolve the tick driver at claim time (PR-26 / M9).

Worker = the active preset admits work AND a live worker holds the kernel flock;
anything else = driverless (``""``), however live the attended loop-slot owner is.
``external`` is never auto-detected. The substrate-agnostic pin flips the fleet
verdict around the SAME detection call and asserts the output tracks it.
"""

import json
import os
from pathlib import Path

import pytest

from teatree.loop.driver_detection import detect_driver
from teatree.utils import singleton as singleton_mod
from teatree.utils.singleton import WORKER_SINGLETON, current_context, singleton

_OWNER_KEY = "t3-loop-tick-owner"  # gitleaks:allow — registry slot name, not a credential


def _set_fleet_admits(monkeypatch: pytest.MonkeyPatch, *, admits: bool) -> None:
    # Patch where it is looked up (driver_detection binds it at import), not its source.
    monkeypatch.setattr("teatree.loop.driver_detection.fleet_admits_work", lambda *a, **k: admits)


def _write_live_owner_record(registry_dir: Path) -> None:
    record = {"session_id": "sess-a", "pid": os.getpid(), "pid_namespace": current_context().pid_namespace}
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / "loop-registry.json").write_text(json.dumps({_OWNER_KEY: record}), encoding="utf-8")


class TestWorkerDetection:
    def test_admitted_fleet_and_held_flock_is_loop_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_fleet_admits(monkeypatch, admits=True)
        monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path)
        with singleton(WORKER_SINGLETON):
            assert detect_driver() == "loop_runner"

    def test_admitted_fleet_but_free_flock_is_driverless(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The "work admitted but nothing running" hole — the DRIVERLESS case.
        _set_fleet_admits(monkeypatch, admits=True)
        monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path)
        assert detect_driver() == ""

    def test_stopped_fleet_with_held_flock_is_not_loop_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_fleet_admits(monkeypatch, admits=False)
        monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path)
        with singleton(WORKER_SINGLETON):
            assert detect_driver() == ""

    def test_a_verdict_read_failure_is_driverless(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _unreadable() -> bool:
            msg = "control DB unreadable"
            raise RuntimeError(msg)

        monkeypatch.setattr("teatree.loop.driver_detection.fleet_admits_work", _unreadable)
        monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path)
        with singleton(WORKER_SINGLETON):
            assert detect_driver() == ""


class TestAnAttendedOwnerDrivesNothing:
    def test_a_live_registry_owner_with_a_free_flock_is_driverless(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_fleet_admits(monkeypatch, admits=True)
        monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        _write_live_owner_record(tmp_path)

        assert detect_driver() == ""

    def test_control_the_same_owner_with_a_held_flock_is_loop_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_fleet_admits(monkeypatch, admits=True)
        monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        _write_live_owner_record(tmp_path)

        with singleton(WORKER_SINGLETON):
            assert detect_driver() == "loop_runner"


class TestSubstrateAgnostic:
    def test_detection_tracks_the_fleet_verdict_around_the_same_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same held flock, same call path — only the live fleet verdict differs, and
        # detection tracks it. No branch references any cron plane.
        monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path)
        with singleton(WORKER_SINGLETON):
            _set_fleet_admits(monkeypatch, admits=False)
            assert detect_driver() == ""
            _set_fleet_admits(monkeypatch, admits=True)
            assert detect_driver() == "loop_runner"
