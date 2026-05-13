"""Pid-file singleton guards for long-running processes.

One teatree instance shares one SQLite DB and one queue of background tasks.
Two concurrent ``t3 <overlay> worker`` invocations would compete for the same
rows and double-execute side effects; two concurrent ``t3 slack listen``
processes would double-ack every Slack event. Both commands wrap their main
loop with :func:`singleton` so a second invocation refuses to start while
the first is alive.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from teatree.paths import DATA_DIR


class AlreadyRunningError(RuntimeError):
    """A live process already holds the named singleton."""

    def __init__(self, name: str, pid: int, pid_path: Path) -> None:
        super().__init__(f"{name} already running (PID {pid}) — see {pid_path}")
        self.name = name
        self.pid = pid
        self.pid_path = pid_path


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def default_pid_path(name: str) -> Path:
    return DATA_DIR / f"{name}.pid"


def read_pid(pid_path: Path) -> int | None:
    """Return the live pid recorded at ``pid_path``, or ``None``.

    Returns ``None`` when the file is missing, malformed, or the pid is dead.
    Removes the file in the malformed/dead cases so the caller can claim it.
    """
    if not pid_path.is_file():
        return None
    raw = pid_path.read_text(encoding="utf-8").strip()
    if not raw.isdigit():
        pid_path.unlink(missing_ok=True)
        return None
    pid = int(raw)
    if not pid_alive(pid):
        pid_path.unlink(missing_ok=True)
        return None
    return pid


@contextmanager
def singleton(name: str, *, pid_path: Path | None = None) -> Iterator[Path]:
    """Acquire a singleton lock named ``name`` for the lifetime of the block.

    Raises :class:`AlreadyRunningError` if another live process owns the lock.
    Writes the current pid on entry and clears it on exit (including crashes).
    """
    path = pid_path or default_pid_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    live_pid = read_pid(path)
    if live_pid is not None:
        raise AlreadyRunningError(name, live_pid, path)
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)
