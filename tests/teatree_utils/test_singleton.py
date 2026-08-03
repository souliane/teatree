"""Tests for ``teatree.utils.singleton`` flock-backed locks."""

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from teatree.utils.singleton import (
    AlreadyRunningError,
    ExecutionContext,
    HolderVerdict,
    current_context,
    default_pid_path,
    flock_is_held,
    holder_verdict,
    pid_alive,
    read_holder,
    read_pid,
    singleton,
)


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

    def test_dead_pid_returns_none_but_preserves_the_lock_file(self, tmp_path: Path) -> None:
        # The lock file is the flock anchor — unlinking it orphans a live holder's
        # kernel lock (the inode), so read_pid never removes it (#3617).
        path = tmp_path / "dead.pid"
        path.write_text("999999999\n", encoding="utf-8")
        assert read_pid(path) is None
        assert path.is_file()

    def test_garbled_pid_returns_none_but_preserves_the_lock_file(self, tmp_path: Path) -> None:
        path = tmp_path / "garbled.pid"
        path.write_text("not-a-number\n", encoding="utf-8")
        assert read_pid(path) is None
        assert path.is_file()

    def test_live_pid_is_returned(self, tmp_path: Path) -> None:
        path = tmp_path / "alive.pid"
        path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        assert read_pid(path) == os.getpid()


class TestSingleton:
    def test_acquires_and_records_pid(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.pid"
        with singleton("test", pid_path=path) as held:
            assert held == path
            assert read_pid(path) == os.getpid()

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
            assert read_pid(path) == os.getpid()

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


class TestFlockIsHeld:
    def test_free_when_no_holder(self, tmp_path: Path) -> None:
        assert flock_is_held("t", pid_path=tmp_path / "t.pid") is False

    def test_held_while_a_singleton_is_active(self, tmp_path: Path) -> None:
        path = tmp_path / "t.pid"
        with singleton("t", pid_path=path):
            assert flock_is_held("t", pid_path=path) is True

    def test_ignores_a_recycled_live_pid_with_no_flock(self, tmp_path: Path) -> None:
        # A live pid recorded in the file but NO flock held: the probe reads the
        # kernel lock (free), never the recyclable pid (which would read "held").
        path = tmp_path / "t.pid"
        path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        assert flock_is_held("t", pid_path=path) is False

    def test_stays_visible_after_a_stale_pid_read(self, tmp_path: Path) -> None:
        # A live worker holds the flock while its lock file records a dead pid (a
        # clobber, or the truncate-write window). A diagnostic `read_pid` reap must
        # not unlink the file — doing so orphans the flock's inode and every later
        # probe opens a fresh inode reading "free", spawning a duplicate worker (#3617).
        path = tmp_path / "t.pid"
        with singleton("t", pid_path=path):
            path.write_text("999999999\n", encoding="utf-8")
            assert read_pid(path) is None
            assert path.is_file()
            assert flock_is_held("t", pid_path=path) is True


#: A holder outside this runtime (a bare-host process) and the containerized refuser —
#: the #3976 shape, where the flock is shared through a bind mount but the pid is not.
_OUTSIDE_HOLDER = ExecutionContext(pid_namespace="pid:[1]", hostname="box", role="")
_IN_CONTAINER = ExecutionContext(pid_namespace="pid:[2]", hostname="box", role="worker")


class TestHolderRecord:
    def test_acquirer_records_its_execution_context_beside_the_pid(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.pid"
        with singleton("t", pid_path=path):
            record = read_holder(path)
        assert record is not None
        assert record.pid == os.getpid()
        assert record.context == current_context()

    def test_pid_line_is_still_the_first_line(self, tmp_path: Path) -> None:
        # `read_pid`, `_recorded_pid` and every operator `cat` read line 1 — the
        # record is appended, never substituted, so no consumer's contract moves.
        path = tmp_path / "lock.pid"
        with singleton("t", pid_path=path):
            assert path.read_text(encoding="utf-8").splitlines()[0] == str(os.getpid())
            assert read_pid(path) == os.getpid()

    def test_legacy_single_line_file_has_no_record(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.pid"
        path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        assert read_pid(path) == os.getpid()
        assert read_holder(path) is None

    def test_garbled_record_reads_as_absent(self, tmp_path: Path) -> None:
        path = tmp_path / "garbled.pid"
        path.write_text(f"{os.getpid()}\nnot-json\n", encoding="utf-8")
        assert read_holder(path) is None

    def test_missing_file_has_no_record(self, tmp_path: Path) -> None:
        assert read_holder(tmp_path / "absent.pid") is None


class TestHolderVerdict:
    def test_same_namespace_is_same_context(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.pid"
        with singleton("t", pid_path=path):
            assert holder_verdict(read_holder(path), current_context()) is HolderVerdict.SAME_CONTEXT

    def test_different_namespace_is_foreign(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.pid"
        with singleton("t", pid_path=path):
            record = read_holder(path)
        elsewhere = ExecutionContext(pid_namespace="pid:[999999]", hostname="other", role="worker")
        assert holder_verdict(record, elsewhere) is HolderVerdict.FOREIGN_CONTEXT

    def test_absent_record_is_unknown(self) -> None:
        assert holder_verdict(None, current_context()) is HolderVerdict.UNKNOWN_CONTEXT

    def test_unreadable_namespace_on_either_side_is_unknown(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.pid"
        with singleton("t", pid_path=path):
            record = read_holder(path)
        blind = ExecutionContext(pid_namespace="", hostname="h", role="")
        assert holder_verdict(record, blind) is HolderVerdict.UNKNOWN_CONTEXT


class TestRefusalMessage:
    def test_names_a_second_copy_of_this_service_when_the_pid_resolves_here(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.pid"
        with (
            singleton("t", pid_path=path),
            pytest.raises(AlreadyRunningError) as exc,
            singleton("t", pid_path=path),
        ):
            pass
        message = str(exc.value)
        assert exc.value.verdict is HolderVerdict.SAME_CONTEXT
        assert "another copy of this service holds it" in message
        assert f"already running (PID {os.getpid()})" in message

    def test_says_the_holder_is_not_resolvable_here_across_a_namespace_split(self, tmp_path: Path) -> None:
        exc = AlreadyRunningError(
            "worker", 4321, tmp_path / "worker.pid", holder_context=_OUTSIDE_HOLDER, context=_IN_CONTAINER
        )
        message = str(exc)
        assert exc.verdict is HolderVerdict.FOREIGN_CONTEXT
        assert "NOT resolvable here" in message
        assert "held from OUTSIDE this runtime" in message
        assert _OUTSIDE_HOLDER.pid_namespace in message
        assert _IN_CONTAINER.pid_namespace in message

    def test_says_the_context_is_unrecorded_when_the_holder_left_none(self, tmp_path: Path) -> None:
        exc = AlreadyRunningError("worker", 4321, tmp_path / "worker.pid")
        assert exc.verdict is HolderVerdict.UNKNOWN_CONTEXT
        assert "recorded no execution context" in str(exc)

    def test_fingerprint_ignores_the_holder_pid_so_a_holder_restart_keeps_the_streak(self, tmp_path: Path) -> None:
        path = tmp_path / "w.pid"
        first = AlreadyRunningError("worker", 11, path, holder_context=_OUTSIDE_HOLDER, context=_IN_CONTAINER)
        second = AlreadyRunningError("worker", 22, path, holder_context=_OUTSIDE_HOLDER, context=_IN_CONTAINER)
        assert first.reason_fingerprint == second.reason_fingerprint

    def test_fingerprint_changes_when_the_holder_context_changes(self, tmp_path: Path) -> None:
        path = tmp_path / "w.pid"
        sibling_container = ExecutionContext(pid_namespace="pid:[3]", hostname="box", role="admin")
        outside = AlreadyRunningError("worker", 11, path, holder_context=_OUTSIDE_HOLDER, context=_IN_CONTAINER)
        sibling = AlreadyRunningError("worker", 11, path, holder_context=sibling_container, context=_IN_CONTAINER)
        assert outside.reason_fingerprint != sibling.reason_fingerprint


class TestCurrentContext:
    def test_reads_the_role_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEATREE_ROLE", "worker")
        assert current_context().role == "worker"

    def test_role_is_blank_off_a_deployment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEATREE_ROLE", raising=False)
        assert current_context().role == ""

    def test_round_trips_through_the_recorded_json(self) -> None:
        context = current_context()
        assert ExecutionContext.from_json(json.loads(json.dumps(context.as_json()))) == context

    def test_unreadable_namespace_link_degrades_to_blank(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A lock primitive must acquire on a kernel with no procfs; a blank namespace
        # is then reported as UNKNOWN rather than matching anything.
        unreadable = OSError("no procfs here")

        def _boom(_self: Path) -> Path:
            raise unreadable

        monkeypatch.setattr("teatree.utils.singleton.Path.readlink", _boom)
        assert current_context().pid_namespace == ""
