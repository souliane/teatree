"""Tests for ``teatree.utils.singleton`` pid-file locks."""

import os
from pathlib import Path

import pytest

from teatree.utils.singleton import AlreadyRunningError, default_pid_path, pid_alive, read_pid, singleton


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
    def test_acquires_and_releases(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.pid"
        with singleton("test", pid_path=path):
            assert path.read_text(encoding="utf-8").strip() == str(os.getpid())
        assert not path.is_file()

    def test_releases_on_exception(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.pid"
        msg = "boom"
        with pytest.raises(RuntimeError), singleton("test", pid_path=path):
            raise RuntimeError(msg)
        assert not path.is_file()

    def test_refuses_second_instance(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.pid"
        path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        with pytest.raises(AlreadyRunningError) as exc, singleton("test", pid_path=path):
            pass
        assert exc.value.pid == os.getpid()
        assert exc.value.name == "test"

    def test_steals_stale_pid(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.pid"
        path.write_text("999999999\n", encoding="utf-8")
        with singleton("test", pid_path=path):
            assert path.read_text(encoding="utf-8").strip() == str(os.getpid())

    def test_default_path_uses_data_dir(self) -> None:
        path = default_pid_path("worker")
        assert path.name == "worker.pid"
