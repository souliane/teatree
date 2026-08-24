"""Flock-backed singleton guards for long-running processes.

One teatree instance shares one SQLite DB and one queue of background
tasks. Two concurrent ``t3 <overlay> worker`` invocations would compete
for the same rows and double-execute side effects, and two concurrent
``t3 slack listen`` processes would double-ack every Slack event. Those
are the entry points that wrap their main loop with :func:`singleton`,
so a second invocation refuses to start while the first is alive.

``t3 loop tick`` is NOT one of them. N concurrent ticks (one per open
Claude Code session, each registered by the session-start hook's
``CronCreate``) are held apart by the DB ``loop-tick`` ``LoopLease``
compare-and-swap; no flock wraps the tick, so an audit of tick
concurrency reads that lease and never a lock file.

The guard is a non-blocking ``fcntl.flock``. It is kernel-enforced:
crash-safe (the lock releases when the holder's process dies, with no
stale-pid window to steal), and free of the read-pid/write-pid TOCTOU
race the previous pid-file implementation had. The lock file still
records the holder's pid so ``t3 doctor`` and ``read_pid`` can report
*who* holds it — but the pid is diagnostic only; the ``flock`` is the
lock.

A pid alone is not enough to say WHERE the holder is (#3976). The lock file
lives on a path the host and the containerized deployment share, so the flock
is genuinely global while the pid is namespace-local: a refusal naming "PID
41234" is unresolvable in the refusing process and reads identically whether a
second copy of the same service holds it or a process outside the deployment
does. Those two need opposite responses, so the acquirer records its
:class:`ExecutionContext` beside the pid and :func:`holder_verdict` compares the
two.
"""

import contextlib
import fcntl
import json
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
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

#: The ``TEATREE_ROLE`` a containerized deployment gives the service that is SUPPOSED
#: to hold :data:`WORKER_SINGLETON`. A bare-host process has no role at all, which is
#: what lets ``t3 doctor`` tell the deployed worker from one started outside it. This
#: is a deployment ROLE, deliberately a separate identity from the singleton NAME above
#: even though the two spell the same word.
DEPLOYMENT_WORKER_ROLE = "worker"

#: Where the kernel publishes this process's pid namespace. Two processes reading the
#: same link value share a pid namespace, so one's pid is resolvable in the other.
_PID_NAMESPACE_LINK = Path("/proc/self/ns/pid")

#: A lock file carrying a holder record is the pid line plus the record line.
_RECORDED_LINES = 2


class HolderVerdict(StrEnum):
    """Whether the holder's pid means anything in the asking process's own context."""

    SAME_CONTEXT = "same-context"
    FOREIGN_CONTEXT = "foreign-context"
    UNKNOWN_CONTEXT = "unknown-context"


@dataclass(frozen=True)
class ExecutionContext:
    """Where a process runs: its pid namespace, its host, and its deployment role.

    ``pid_namespace`` is the comparison key and the only one — it is the kernel's own
    answer, so it cannot collide the way two boxes sharing a hostname can. The other
    two fields are descriptive: they turn "a different namespace" into a sentence an
    operator can act on, and ``role`` is what says whether the process is part of the
    deployment at all.
    """

    pid_namespace: str
    hostname: str
    role: str

    def as_json(self) -> dict[str, str]:
        return {"pid_namespace": self.pid_namespace, "hostname": self.hostname, "role": self.role}

    @classmethod
    def from_json(cls, payload: dict[str, str]) -> "ExecutionContext":
        return cls(
            pid_namespace=str(payload.get("pid_namespace", "")),
            hostname=str(payload.get("hostname", "")),
            role=str(payload.get("role", "")),
        )

    def describe(self) -> str:
        return f"{self.pid_namespace or 'unknown-namespace'} (role={self.role or 'none'}, host={self.hostname})"


@dataclass(frozen=True)
class HolderRecord:
    """The pid holding a singleton, plus the context that pid is resolvable in."""

    pid: int
    context: ExecutionContext


def current_context(env: dict[str, str] | None = None) -> ExecutionContext:
    """This process's execution context — the one written under the lock on acquire.

    Degrades to a blank ``pid_namespace`` where procfs is unreadable rather than
    raising: a lock primitive must acquire on any kernel, and a blank namespace is
    reported as an explicit UNKNOWN verdict rather than a false match.
    """
    resolved = env if env is not None else dict(os.environ)
    try:
        namespace = str(_PID_NAMESPACE_LINK.readlink())
    except OSError:
        namespace = ""
    return ExecutionContext(
        pid_namespace=namespace,
        hostname=socket.gethostname(),
        role=resolved.get("TEATREE_ROLE", "").strip(),
    )


def read_holder(pid_path: Path) -> HolderRecord | None:
    """The holder record at ``pid_path``, or ``None`` when it is absent or unreadable.

    ``None`` is the honest answer for a legacy one-line lock file and for a garbled
    record alike — both mean "this holder cannot be attributed", which callers report
    as such instead of guessing.
    """
    try:
        lines = pid_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < _RECORDED_LINES or not lines[0].strip().isdigit():
        return None
    try:
        payload = json.loads(lines[1])
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return HolderRecord(pid=int(lines[0].strip()), context=ExecutionContext.from_json(payload))


def holder_verdict(record: HolderRecord | None, context: ExecutionContext) -> HolderVerdict:
    """Whether ``record``'s pid is resolvable in ``context``.

    A missing record, or a blank namespace on either side, is UNKNOWN — never
    optimistically SAME. Guessing "same" is what makes a cross-boundary holder read as
    a second copy of the same service.
    """
    if record is None or not record.context.pid_namespace or not context.pid_namespace:
        return HolderVerdict.UNKNOWN_CONTEXT
    if record.context.pid_namespace == context.pid_namespace:
        return HolderVerdict.SAME_CONTEXT
    return HolderVerdict.FOREIGN_CONTEXT


def _verdict_clause(verdict: HolderVerdict, pid: int, holder: ExecutionContext | None, mine: ExecutionContext) -> str:
    if verdict is HolderVerdict.SAME_CONTEXT:
        return f"another copy of this service holds it — PID {pid} runs in this process's own {mine.describe()}"
    if verdict is HolderVerdict.FOREIGN_CONTEXT:
        holder_desc = holder.describe() if holder is not None else "an unrecorded context"
        return (
            f"the holder is NOT resolvable here — PID {pid} belongs to {holder_desc}, "
            f"not this process's {mine.describe()}; the singleton is held from OUTSIDE this runtime"
        )
    return (
        f"the holder recorded no execution context, so PID {pid} cannot be resolved from this process's "
        f"{mine.describe()} — treat it as possibly outside this runtime"
    )


class AlreadyRunningError(RuntimeError):
    """A live process already holds the named singleton.

    Carries the verdict on WHERE that process is, and a
    :attr:`reason_fingerprint` a restart loop can compare across process lifetimes to
    tell "the same refusal, again" from a new one. The fingerprint deliberately omits
    the holder's pid: a foreign holder restarting does not change the reason.
    """

    def __init__(
        self,
        name: str,
        pid: int,
        pid_path: Path,
        *,
        holder_context: ExecutionContext | None = None,
        context: ExecutionContext | None = None,
    ) -> None:
        mine = context if context is not None else current_context()
        record = HolderRecord(pid=pid, context=holder_context) if holder_context is not None else None
        verdict = holder_verdict(record, mine)
        super().__init__(
            f"{name} already running (PID {pid}) — see {pid_path}. "
            f"{_verdict_clause(verdict, pid, holder_context, mine)}."
        )
        self.name = name
        self.pid = pid
        self.pid_path = pid_path
        self.verdict = verdict
        self.holder_context = holder_context
        self.context = mine
        self.reason_fingerprint = "|".join(
            (
                name,
                verdict.value,
                holder_context.pid_namespace if holder_context else "",
                holder_context.role if holder_context else "",
                holder_context.hostname if holder_context else "",
                mine.pid_namespace,
                mine.role,
            )
        )


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


def _first_line(text: str) -> str:
    """The pid line. Line 1 is the pid and always has been; the holder record follows."""
    lines = text.splitlines()
    return lines[0].strip() if lines else ""


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
    raw = _first_line(pid_path.read_text(encoding="utf-8"))
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
        raw = _first_line(path.read_text(encoding="utf-8"))
    except OSError:
        return 0
    return int(raw) if raw.isdigit() else 0


@contextmanager
def singleton(name: str, *, pid_path: Path | None = None) -> Iterator[Path]:
    """Acquire a singleton lock named ``name`` for the lifetime of the block.

    Raises :class:`AlreadyRunningError` if another live process owns the
    lock — carrying the holder's recorded context, so the refusal says WHERE
    the holder is rather than only naming a pid. The kernel releases the lock
    on context exit OR on process death — there is no stale state to clean up.
    The lock file is NOT unlinked on exit (unlinking a path another opener may
    have already ``open()``-ed reintroduces a double-acquire race); it is
    reused in place by the next acquirer.
    """
    path = pid_path or default_pid_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            holder = read_holder(path)
            recorded = _recorded_pid(path)
            os.close(fd)
            raise AlreadyRunningError(
                name,
                holder.pid if holder is not None else recorded,
                path,
                holder_context=holder.context if holder is not None else None,
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n{json.dumps(current_context().as_json())}\n".encode())
        try:
            yield path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
