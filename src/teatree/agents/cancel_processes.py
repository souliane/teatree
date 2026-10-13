"""Inspect a cancelled run's checkout processes before releasing or stopping them."""

import ctypes
import errno
import os
import platform
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from teatree.core.cleanup.process_table import venue_proc_root
from teatree.utils.agent_spawn_marker import DISPATCHED_AGENT_ENV

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
            target = fd.readlink()
            if not _under(target, checkout):
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
    owned: list[CheckoutProcess] = []
    for entry in entries:
        pid = int(entry.name)
        detail = details.get(pid)
        if detail is None or pid == os.getpid():
            continue
        try:
            cwd = entry.joinpath("cwd").readlink()
            if not _under(cwd, checkout):
                continue
            marker = f"{DISPATCHED_AGENT_ENV}=1".encode() in (entry / "environ").read_bytes().split(b"\0")
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        descendant = _descends(pid, parents, os.getpid())
        if not (descendant or marker):
            continue
        shared_server = bool(
            argv and Path(os.fsdecode(argv[0])).name == "codex" and len(argv) > 1 and argv[1] == b"app-server"
        )
        owned.append(
            CheckoutProcess(
                pid=pid,
                started=detail[1],
                descendant=descendant,
                marker=marker,
                writer=_writer(entry, checkout),
                shared_server=shared_server,
            )
        )
    return tuple(sorted(owned, key=lambda process: process.pid))


def _syscall(number: int, *args: int | None) -> int:
    if platform.system() != "Linux":
        raise OSError(errno.ENOSYS, "pidfd requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    result = int(libc.syscall(number, *args))
    if result == -1:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return result


def _pidfd_open(pid: int) -> int:
    return _syscall(_PIDFD_OPEN, pid, 0)


def _pidfd_signal(descriptor: int) -> None:
    _syscall(_PIDFD_SEND_SIGNAL, descriptor, signal.SIGTERM, None, 0)


def wait_for_owned_checkout_processes(checkout: Path, *, beat_seconds: float) -> tuple[CheckoutProcess, ...]:
    for _ in range(2):
        remaining = owned_checkout_processes(checkout)
        if not remaining:
            return ()
        time.sleep(beat_seconds)
    return owned_checkout_processes(checkout)


def stop_owned_writers(checkout: Path) -> tuple[int, ...]:
    """Signal only descendants still showing checkout write evidence after pidfd opens."""
    signalled: list[int] = []
    for process in owned_checkout_processes(checkout):
        if not (process.descendant and process.writer) or process.shared_server:
            continue
        try:
            descriptor = _pidfd_open(process.pid)
        except OSError:
            continue
        try:
            current = next((item for item in owned_checkout_processes(checkout) if item.pid == process.pid), None)
            if current != process or not current.descendant or not current.writer or current.shared_server:
                continue
            _pidfd_signal(descriptor)
            signalled.append(process.pid)
        except OSError:
            continue
        finally:
            os.close(descriptor)
    return tuple(signalled)
