"""Flock-backed singleton guards for long-running processes.

One teatree instance shares one SQLite DB and one queue of background
tasks. Two concurrent ``t3 <overlay> worker`` invocations would compete
for the same rows and double-execute side effects; two concurrent
``t3 slack listen`` processes would double-ack every Slack event; and N
concurrent ``t3 loop tick`` processes (one per open Claude Code session,
each registered by the session-start hook's ``CronCreate``) would race
on scanner state, the statusline file, and per-row dispatch dedup. Each
of these wraps its main loop with :func:`singleton` so a second
invocation refuses to start while the first is alive.

The guard is a non-blocking ``fcntl.flock``. It is kernel-enforced:
crash-safe (the lock releases when the holder's process dies, with no
stale-pid window to steal), and free of the read-pid/write-pid TOCTOU
race the previous pid-file implementation had. The lock file still
records the holder's pid so ``t3 doctor`` and ``read_pid`` can report
*who* holds it — but the pid is diagnostic only; the ``flock`` is the
lock.
"""

import contextlib
import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from teatree.paths import DATA_DIR

#: The #1796 :class:`~teatree.loops.worker.LoopWorker` flock singleton name — at most
#: one worker drains the shared queue per box. Homed here, next to the singleton
#: mechanism, so the worker's acquire and the tick drain's stand-down probe read the
#: SAME constant without a cross-layer import between ``teatree.loops`` and
#: ``teatree.loop``. The ``t3 <overlay> worker`` db_worker spawner acquires it too
#: (PR-28 completed the #5 deprecation: the pre-#1796 ``teatree-worker`` singleton is
#: gone), so at most one worker of ANY kind drains the shared queue.
WORKER_SINGLETON = "worker"


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

    Diagnostic helper (consumed by ``t3 doctor``). Returns ``None`` when the
    file is missing, malformed, or the recorded pid is dead. It NEVER unlinks
    the file: the lock file is the ``flock`` anchor, so removing it orphans a
    live holder's kernel lock on the (now unlinked) inode — every later
    :func:`flock_is_held` probe then opens a fresh inode, reads "free", and a
    second worker acquires the singleton next to the live one (#3617). The
    stale pid is harmless: the next acquirer reuses the file in place
    (``ftruncate`` + rewrite in :func:`singleton`).
    """
    if not pid_path.is_file():
        return None
    raw = pid_path.read_text(encoding="utf-8").strip()
    if not raw.isdigit():
        return None
    pid = int(raw)
    if not pid_alive(pid):
        return None
    return pid


def flock_is_held(name: str, *, pid_path: Path | None = None) -> bool:
    """Whether a live process holds the ``name`` singleton flock, right now.

    A non-blocking ``flock`` probe against the KERNEL lock state — not the recorded
    pid — so a recycled/stale pid can never make a dead holder look alive (the TOCTOU
    hazard a ``read_pid`` liveness probe has: an unrelated live process that reused a
    crashed worker's pid would suppress resurrection indefinitely). Opens the lock
    file and tries a non-blocking ``LOCK_EX``: acquiring means no holder (the lock is
    released again immediately), ``BlockingIOError`` means a live holder. The file is
    never unlinked (the same reuse-in-place contract as :func:`singleton`).
    """
    path = pid_path or default_pid_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _recorded_pid(path: Path) -> int:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    return int(raw) if raw.isdigit() else 0


@contextmanager
def singleton(name: str, *, pid_path: Path | None = None) -> Iterator[Path]:
    """Acquire a singleton lock named ``name`` for the lifetime of the block.

    Raises :class:`AlreadyRunningError` if another live process owns the
    lock. The kernel releases the lock on context exit OR on process
    death — there is no stale state to clean up. The lock file is NOT
    unlinked on exit (unlinking a path another opener may have already
    ``open()``-ed reintroduces a double-acquire race); it is reused in
    place by the next acquirer.
    """
    path = pid_path or default_pid_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            holder = _recorded_pid(path)
            os.close(fd)
            raise AlreadyRunningError(name, holder, path) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        try:
            yield path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
