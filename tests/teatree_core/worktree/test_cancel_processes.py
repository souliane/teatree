import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from teatree.core.cleanup.process_table import venue_proc_root
from teatree.core.worktree.cancel_processes import (
    _PIDFD_OPEN,
    StopResult,
    owned_checkout_processes,
    stop_owned_writers,
    stop_recorded_writers,
    wait_for_owned_checkout_processes,
)


@dataclass(frozen=True)
class _ProcessSpec:
    pid: int
    parent: int
    marker: bool = False
    writer: bool = False
    command: bytes = b"sleep\0"


def _process(root: Path, checkout: Path, spec: _ProcessSpec) -> None:
    entry = root / str(spec.pid)
    entry.mkdir()
    (entry / "stat").write_text(f"{spec.pid} (test) S {spec.parent} 1 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 100 0 0\n")
    (entry / "cwd").symlink_to(checkout)
    (entry / "environ").write_bytes(b"T3_DISPATCHED_AGENT=1\0" if spec.marker else b"")
    (entry / "cmdline").write_bytes(spec.command)
    (entry / "fd").mkdir()
    (entry / "fdinfo").mkdir()
    if spec.writer:
        (entry / "fd" / "3").symlink_to(checkout / "changed.txt")
        (entry / "fdinfo" / "3").write_text("flags:\t0100001\n")


def _fake_pidfd(sent: list[int], on_open: Callable[[int], None] | None = None) -> Callable[..., int]:
    def syscall(number: int, target: int, *rest: object) -> int:
        if number == _PIDFD_OPEN:
            if on_open is not None:
                on_open(target)
            return target + 1000
        assert rest[0] == signal.SIGTERM
        sent.append(target)
        return 0

    return syscall


def test_wait_sees_descendants_and_marker_orphans_but_stop_only_signals_descendant_writers() -> None:
    with TemporaryDirectory() as temp:
        root = Path(temp) / "proc"
        checkout = Path(temp) / "checkout"
        root.mkdir()
        checkout.mkdir()
        _process(root, checkout, _ProcessSpec(101, 100, writer=True))
        _process(root, checkout, _ProcessSpec(102, 1, marker=True, writer=True))
        _process(root, checkout, _ProcessSpec(103, 1, writer=True))
        _process(root, checkout, _ProcessSpec(104, 100, writer=True, command=b"codex\0app-server\0"))
        _process(root, checkout, _ProcessSpec(105, 100))
        _process(root, checkout, _ProcessSpec(106, 101, writer=True))
        with (
            patch("teatree.core.worktree.cancel_processes.venue_proc_root", return_value=root),
            patch("teatree.core.worktree.cancel_processes.os.getpid", return_value=100),
            patch("teatree.core.worktree.cancel_processes._syscall", side_effect=_fake_pidfd(send := [])),
            patch("teatree.core.worktree.cancel_processes.os.close"),
            patch("teatree.core.worktree.cancel_processes.os.kill") as kill,
        ):
            assert {process.pid for process in owned_checkout_processes(checkout)} == {101, 102, 104, 105, 106}
            assert stop_owned_writers(checkout) == (101, 106)
        assert send == [1101, 1106]
        kill.assert_not_called()


def test_recheck_after_pidfd_open_refuses_process_that_lost_write_evidence() -> None:
    with TemporaryDirectory() as temp:
        root = Path(temp) / "proc"
        checkout = Path(temp) / "checkout"
        root.mkdir()
        checkout.mkdir()
        _process(root, checkout, _ProcessSpec(101, 100, writer=True))

        def remove_writer(pid: int) -> None:
            (root / str(pid) / "fd" / "3").unlink()

        with (
            patch("teatree.core.worktree.cancel_processes.venue_proc_root", return_value=root),
            patch("teatree.core.worktree.cancel_processes.os.getpid", return_value=100),
            patch(
                "teatree.core.worktree.cancel_processes._syscall", side_effect=_fake_pidfd(send := [], remove_writer)
            ),
            patch("teatree.core.worktree.cancel_processes.os.close"),
        ):
            assert stop_owned_writers(checkout) == ()
        assert send == []


def test_owner_approved_stop_uses_captured_identity_after_runner_exit() -> None:
    with TemporaryDirectory() as temp:
        root = Path(temp) / "proc"
        checkout = Path(temp) / "checkout"
        root.mkdir()
        checkout.mkdir()
        _process(root, checkout, _ProcessSpec(101, 1, writer=True))
        with (
            patch("teatree.core.worktree.cancel_processes.venue_proc_root", return_value=root),
            patch("teatree.core.worktree.cancel_processes.current_pid_namespace", return_value="pid:[1]"),
            patch("teatree.core.worktree.cancel_processes._syscall", side_effect=_fake_pidfd(send := [])),
            patch("teatree.core.worktree.cancel_processes.os.close"),
        ):
            assert stop_recorded_writers(checkout, ((101, 0),), "pid:[1]").stopped == (101,)
            assert stop_recorded_writers(checkout, ((101, 2),), "pid:[1]") == StopResult()
            other = stop_recorded_writers(checkout, ((101, 0),), "pid:[other]")
        assert send == [1101]
        assert other.stopped == ()
        assert "pid:[other]" in other.refused


def test_wait_checks_twice_then_returns_survivors() -> None:
    checkout = Path("/checkout")
    survivor = object()
    with (
        patch("teatree.core.worktree.cancel_processes.owned_checkout_processes", return_value=(survivor,)) as scan,
        patch("teatree.core.worktree.cancel_processes.time.sleep") as sleep,
    ):
        assert wait_for_owned_checkout_processes(checkout, beat_seconds=0.01) == (survivor,)
    assert scan.call_count == 3
    assert sleep.call_count == 2


def _writer_in(cwd: Path) -> subprocess.Popen[bytes]:
    holds_a_write = "import time; handle = open('partial.txt', 'w'); print('ready', flush=True); time.sleep(60)"
    process = subprocess.Popen([sys.executable, "-c", holds_a_write], cwd=cwd, stdout=subprocess.PIPE)
    assert process.stdout is not None
    assert process.stdout.readline() == b"ready\n"
    return process


@pytest.mark.skipif(sys.platform != "linux" or venue_proc_root() is None, reason="needs a Linux /proc and pidfd")
def test_a_real_child_writing_its_checkout_gets_sigterm_through_its_pidfd(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    child, sibling = _writer_in(checkout), _writer_in(tmp_path)
    try:
        assert [process.pid for process in owned_checkout_processes(checkout)] == [child.pid]
        with patch("teatree.core.worktree.cancel_processes.os.kill") as kill:
            assert stop_owned_writers(checkout) == (child.pid,)
        assert child.wait(timeout=10) == -signal.SIGTERM
        assert sibling.poll() is None
        kill.assert_not_called()
    finally:
        for process in (child, sibling):
            process.kill()
            process.communicate()
