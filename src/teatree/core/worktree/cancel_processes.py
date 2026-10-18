"""Inspect checkout processes and stop owned survivors at run exit."""

import ctypes
import errno
import os
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from teatree.core.cleanup.process_table import current_pid_namespace, venue_proc_root
from teatree.utils.agent_spawn_marker import DISPATCHED_AGENT_ENV

OWN_PROCESS_BEAT_SECONDS = 60
# The uv-managed CPython builds ship without os.pidfd_open / signal.pidfd_send_signal; the numbers are arch-unified.
_PIDFD_OPEN = 434
_PIDFD_SEND_SIGNAL = 424


@dataclass(frozen=True, slots=True)
class CheckoutProcess:
    pid: int
    started: int
    descendant: bool
    marker: bool
    writer: bool
    shared_server: bool

    @property
    def stoppable(self) -> bool:
        return self.descendant and self.writer and not self.shared_server

    @property
    def stoppable_on_exit(self) -> bool:
        return (self.descendant or self.marker) and not self.shared_server


@dataclass(frozen=True, slots=True)
class StopResult:
    stopped: tuple[int, ...] = ()
    refused: str = ""


def _under(path: Path, checkout: Path) -> bool:
    return path == checkout or checkout in path.parents


def _details(pid_dir: Path) -> tuple[int, int] | None:
    try:
        fields = (pid_dir / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return int(fields[1]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _writer(pid_dir: Path, checkout: Path) -> bool:
    try:
        fds = tuple((pid_dir / "fd").iterdir())
    except OSError:
        return False
    for fd in fds:
        try:
            if not _under(fd.readlink(), checkout):
                continue
            flags = next(
                line.split(":", 1)[1].strip()
                for line in (pid_dir / "fdinfo" / fd.name).read_text(encoding="utf-8").splitlines()
                if line.startswith("flags:")
            )
            if int(flags, 8) & os.O_ACCMODE in {os.O_WRONLY, os.O_RDWR}:
                return True
        except (OSError, ValueError, StopIteration):
            continue
    return False


def _descends(pid: int, parents: dict[int, int], root_pid: int) -> bool:
    seen: set[int] = set()
    while pid in parents and pid not in seen:
        seen.add(pid)
        pid = parents[pid]
        if pid == root_pid:
            return True
    return False


def _checkout_process(pid_dir: Path, checkout: Path, *, started: int, descendant: bool) -> CheckoutProcess | None:
    try:
        if not _under(pid_dir.joinpath("cwd").readlink(), checkout):
            return None
        marker = f"{DISPATCHED_AGENT_ENV}=1".encode() in (pid_dir / "environ").read_bytes().split(b"\0")
        argv = (pid_dir / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    return CheckoutProcess(
        pid=int(pid_dir.name),
        started=started,
        descendant=descendant,
        marker=marker,
        writer=_writer(pid_dir, checkout),
        shared_server=Path(os.fsdecode(argv[0])).name == "codex" and argv[1:2] == [b"app-server"],
    )


def owned_checkout_processes(checkout: Path) -> tuple[CheckoutProcess, ...]:
    root = venue_proc_root()
    if root is None:
        return ()
    try:
        entries = tuple(entry for entry in root.iterdir() if entry.name.isdigit())
    except OSError:
        return ()
    details = {int(entry.name): detail for entry in entries if (detail := _details(entry)) is not None}
    parents = {pid: detail[0] for pid, detail in details.items()}
    checkout = checkout.resolve()
    owned = []
    for entry in entries:
        pid = int(entry.name)
        if pid not in details or pid == os.getpid():
            continue
        process = _checkout_process(
            entry, checkout, started=details[pid][1], descendant=_descends(pid, parents, os.getpid())
        )
        if process is not None and (process.descendant or process.marker):
            owned.append(process)
    return tuple(sorted(owned, key=lambda process: process.pid))


def wait_for_owned_checkout_processes(
    checkout: Path, *, beat_seconds: float = OWN_PROCESS_BEAT_SECONDS
) -> tuple[CheckoutProcess, ...]:
    for _ in range(2):
        if not owned_checkout_processes(checkout):
            return ()
        time.sleep(beat_seconds)
    return owned_checkout_processes(checkout)


def finish_owned_checkout_processes(
    checkout: Path, *, beat_seconds: float = OWN_PROCESS_BEAT_SECONDS
) -> tuple[CheckoutProcess, ...]:
    def same_identity(process: CheckoutProcess) -> bool:
        return any(
            (current.pid, current.started) == (process.pid, process.started) and not current.shared_server
            for current in owned_checkout_processes(checkout)
        )

    survivors = wait_for_owned_checkout_processes(checkout, beat_seconds=beat_seconds)
    if not survivors:
        return ()
    for process in survivors:
        if process.stoppable_on_exit:
            _stop_if(process.pid, lambda process=process: same_identity(process))
    if not any(process.stoppable_on_exit for process in survivors):
        return survivors
    time.sleep(beat_seconds)
    survivors = owned_checkout_processes(checkout)
    for process in survivors:
        if process.stoppable_on_exit:
            _stop_if(
                process.pid,
                lambda process=process: same_identity(process),
                stop_signal=signal.SIGKILL,
            )
    return wait_for_owned_checkout_processes(checkout, beat_seconds=beat_seconds)


def _syscall(number: int, *args: int | None) -> int:
    if sys.platform != "linux":
        raise OSError(errno.ENOSYS, "pidfd needs Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    if (result := libc.syscall(number, *args)) == -1:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    return result


def _stop_if(pid: int, still_stoppable: Callable[[], bool], *, stop_signal: signal.Signals = signal.SIGTERM) -> bool:
    """Signal *pid* through a pidfd after re-checking its identity."""
    try:
        descriptor = _syscall(_PIDFD_OPEN, pid, 0)
    except OSError:
        return False
    try:
        if still_stoppable():
            _syscall(_PIDFD_SEND_SIGNAL, descriptor, stop_signal, None, 0)
            return True
    except OSError:
        pass
    finally:
        os.close(descriptor)
    return False


def stop_recorded_writers(checkout: Path, candidates: tuple[tuple[int, int], ...], namespace: str) -> StopResult:
    root = venue_proc_root()
    if root is None or not namespace or current_pid_namespace() != namespace:
        return StopResult(refused=f"the recorded pids belong to pid namespace {namespace or 'unknown'}, not this one")
    checkout = checkout.resolve()

    def recorded_writer(pid: int, started: int) -> bool:
        detail = _details(root / str(pid))
        process = _checkout_process(root / str(pid), checkout, started=started, descendant=True)
        return detail is not None and detail[1] == started and process is not None and process.stoppable

    return StopResult(
        stopped=tuple(pid for pid, started in candidates if _stop_if(pid, partial(recorded_writer, pid, started)))
    )


def stop_owned_writers(checkout: Path) -> tuple[int, ...]:
    """Signal only descendants still showing checkout write evidence after their pidfd opens."""

    def unchanged(process: CheckoutProcess) -> bool:
        current = next((item for item in owned_checkout_processes(checkout) if item.pid == process.pid), None)
        return current == process and current.stoppable

    return tuple(
        process.pid
        for process in owned_checkout_processes(checkout)
        if _stop_if(process.pid, partial(unchanged, process))
    )
