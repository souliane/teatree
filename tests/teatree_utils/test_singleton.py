"""Tests for ``teatree.utils.singleton`` flock-backed locks."""

import multiprocessing
import os
import time
from pathlib import Path

import pytest

from teatree.utils.singleton import AlreadyRunningError, default_pid_path, pid_alive, read_pid, singleton


def _hold_lock(lock_path: str, ready_path: str, release_path: str) -> None:
    """Helper: acquire the lock, signal ready, hold until told to release."""
    with singleton("xproc", pid_path=Path(lock_path)):
        Path(ready_path).write_text("acquired", encoding="utf-8")
        deadline = time.time() + 10.0
        while not Path(release_path).exists() and time.time() < deadline:
            time.sleep(0.02)


class TestPidAlive:
    def test_current_process_is_alive(self) -> None:
        assert pid_alive(os.getpid()) is True

    def test_unused_pid_is_dead(self) -> None:
        assert pid_alive(999_999_999) is False


class TestReadPid:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert read_pid(tmp_path / "absent.pid") is None

    def test_dead_pid_is_cleaned(self, tmp_path: Path) -> None:
        path = tmp_path / "dead.pid"
        path.write_text("999999999\n", encoding="utf-8")
        assert read_pid(path) is None
        assert not path.is_file()

    def test_garbled_pid_is_cleaned(self, tmp_path: Path) -> None:
        path = tmp_path / "garbled.pid"
        path.write_text("not-a-number\n", encoding="utf-8")
        assert read_pid(path) is None
        assert not path.is_file()

    def test_live_pid_is_returned(self, tmp_path: Path) -> None:
        path = tmp_path / "alive.pid"
        path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        assert read_pid(path) == os.getpid()


class TestSingleton:
    def test_acquires_and_records_pid(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.pid"
        with singleton("test", pid_path=path) as held:
            assert held == path
            assert path.read_text(encoding="utf-8").strip() == str(os.getpid())

    def test_reacquirable_after_clean_exit(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.pid"
        with singleton("test", pid_path=path):
            pass
        with singleton("test", pid_path=path) as held:
            assert held == path

    def test_reacquirable_after_exception(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.pid"
        msg = "boom"
        with pytest.raises(RuntimeError, match=msg), singleton("test", pid_path=path):
            raise RuntimeError(msg)
        with singleton("test", pid_path=path) as held:
            assert held == path

    def test_nested_acquire_in_same_process_refuses(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.pid"
        with (
            singleton("test", pid_path=path),
            pytest.raises(AlreadyRunningError) as exc,
            singleton("test", pid_path=path),
        ):
            pass
        assert exc.value.pid == os.getpid()
        assert exc.value.name == "test"

    def test_ignores_dead_pid_in_lockfile(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.pid"
        path.write_text("999999999\n", encoding="utf-8")
        with singleton("test", pid_path=path):
            assert path.read_text(encoding="utf-8").strip() == str(os.getpid())

    def test_concurrent_process_is_refused(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock.pid"
        ready_path = tmp_path / "ready"
        release_path = tmp_path / "release"
        proc = multiprocessing.Process(
            target=_hold_lock,
            args=(str(lock_path), str(ready_path), str(release_path)),
        )
        proc.start()
        try:
            deadline = time.time() + 5.0
            while not ready_path.exists() and time.time() < deadline:
                time.sleep(0.02)
            assert ready_path.exists(), "helper never acquired the lock"

            with pytest.raises(AlreadyRunningError) as exc, singleton("xproc", pid_path=lock_path):
                pass
            assert exc.value.pid == proc.pid
        finally:
            release_path.write_text("go", encoding="utf-8")
            proc.join(timeout=5)

    def test_default_path_uses_data_dir(self) -> None:
        path = default_pid_path("worker")
        assert path.name == "worker.pid"
