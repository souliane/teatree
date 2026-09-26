"""``detect_driver`` — resolve the tick driver at claim time (PR-26 / M9).

Worker = the active preset admits work AND a live worker holds the kernel flock;
self-pump = the loop-registry ``t3-loop-tick-owner`` record naming this session
with a live pid; anything else = driverless (``""``). ``external`` is never
auto-detected. The substrate-agnostic pin flips the fleet verdict around the SAME
detection call and asserts the output tracks it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from teatree.loop.driver_detection import detect_driver
from teatree.utils import singleton as singleton_mod
from teatree.utils.singleton import WORKER_SINGLETON, singleton

_OWNER_KEY = "t3-loop-tick-owner"  # gitleaks:allow — registry slot name, not a credential


def _set_fleet_admits(monkeypatch: pytest.MonkeyPatch, *, admits: bool) -> None:
    # Patch where it is looked up (driver_detection binds it at import), not its source.
    monkeypatch.setattr("teatree.loop.driver_detection.fleet_admits_work", lambda *a, **k: admits)


def _write_owner_record(registry_dir: Path, *, session_id: str, pid: int, pid_namespace: str | None = None) -> None:
    """Write the tick-owner record; ``pid_namespace=None`` writes a legacy record without one."""
    record = {"session_id": session_id, "pid": pid}
    if pid_namespace is not None:
        record["pid_namespace"] = pid_namespace
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / "loop-registry.json").write_text(json.dumps({_OWNER_KEY: record}), encoding="utf-8")


def _reading_from(monkeypatch: pytest.MonkeyPatch, namespace: str) -> None:
    """Pin the pid namespace the detection resolves the record's pid in."""
    monkeypatch.setattr("teatree.core.loop_lease_liveness.reader_pid_namespace", lambda: namespace)


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class TestWorkerDetection:
    def test_admitted_fleet_and_held_flock_is_loop_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_fleet_admits(monkeypatch, admits=True)
        monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path)
        with singleton(WORKER_SINGLETON):
            assert detect_driver("sess-a") == "loop_runner"

    def test_admitted_fleet_but_free_flock_is_driverless(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The "work admitted but nothing running" hole — the DRIVERLESS case.
        _set_fleet_admits(monkeypatch, admits=True)
        monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        assert detect_driver("sess-a") == ""

    def test_stopped_fleet_with_held_flock_is_not_loop_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_fleet_admits(monkeypatch, admits=False)
        monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        with singleton(WORKER_SINGLETON):
            assert detect_driver("sess-a") == ""


class TestSelfPumpDetection:
    def test_registry_record_for_this_session_with_live_pid_is_self_pump(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_fleet_admits(monkeypatch, admits=False)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        _write_owner_record(tmp_path, session_id="sess-a", pid=os.getpid())
        assert detect_driver("sess-a") == "self_pump"

    def test_registry_record_for_different_session_is_not_self_pump(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_fleet_admits(monkeypatch, admits=False)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        _write_owner_record(tmp_path, session_id="other", pid=os.getpid())
        assert detect_driver("sess-a") == ""

    def test_registry_record_with_dead_pid_is_not_self_pump(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_fleet_admits(monkeypatch, admits=False)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        _write_owner_record(tmp_path, session_id="sess-a", pid=_dead_pid())
        assert detect_driver("sess-a") == ""

    def test_empty_session_is_never_self_pump(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_fleet_admits(monkeypatch, admits=False)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        _write_owner_record(tmp_path, session_id="", pid=os.getpid())
        assert detect_driver("") == ""


class TestSelfPumpNamespaceAttribution:
    """A registry pid is namespace-local, so a foreign record names nothing here (#4270)."""

    def test_a_record_from_another_pid_namespace_is_not_self_pump(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # This process's own pid, recorded by a sibling container: alive here, and
        # about a different process entirely — a collision, not evidence.
        _set_fleet_admits(monkeypatch, admits=False)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        _write_owner_record(tmp_path, session_id="sess-a", pid=os.getpid(), pid_namespace="pid:[4026532000]")
        _reading_from(monkeypatch, "pid:[4026531000]")

        assert detect_driver("sess-a") == ""

    def test_a_record_from_this_pid_namespace_is_still_self_pump(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_fleet_admits(monkeypatch, admits=False)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        _write_owner_record(tmp_path, session_id="sess-a", pid=os.getpid(), pid_namespace="pid:[4026531000]")
        _reading_from(monkeypatch, "pid:[4026531000]")

        assert detect_driver("sess-a") == "self_pump"

    def test_a_legacy_record_without_a_namespace_still_probes_its_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Blank-tolerant, unlike the dream lease's release: this read only reports a
        # driver, and the record is rewritten with a namespace on the next SessionStart.
        _set_fleet_admits(monkeypatch, admits=False)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        _write_owner_record(tmp_path, session_id="sess-a", pid=os.getpid())
        _reading_from(monkeypatch, "pid:[4026531000]")

        assert detect_driver("sess-a") == "self_pump"


class TestDriverless:
    def test_no_worker_no_registry_is_driverless(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_fleet_admits(monkeypatch, admits=False)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        assert detect_driver("sess-a") == ""


class TestSubstrateAgnostic:
    def test_detection_tracks_the_fleet_verdict_around_the_same_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same held flock, same session, same call path — only the live fleet verdict
        # differs, and detection tracks it. No branch references any cron plane.
        monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
        with singleton(WORKER_SINGLETON):
            _set_fleet_admits(monkeypatch, admits=False)
            assert detect_driver("sess-a") == ""
            _set_fleet_admits(monkeypatch, admits=True)
            assert detect_driver("sess-a") == "loop_runner"
