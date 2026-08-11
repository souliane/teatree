"""Which paths a live process holds open, read from a process table (#4165).

The retention sweep in :mod:`~teatree.core.retention.scratch` is the consumer; this
module is only the probe, so the two concerns move independently — the sweep decides
what is stale, this decides what is held.

FAIL-CLOSED, and the witness is RESOLUTION rather than listability. Listing a pid's
``fd`` directory is the cheap outer read; resolving each entry is the expensive inner
one that actually yields a held path. Scoring the outer success as the answer reports
an unreadable world as an empty one — measured at uid 1001 against a bind-mounted host
``/proc``, 35 of 325 pids present an ``fd`` or ``map_files`` directory this uid can
LIST while every ``readlink()`` inside it raises.
"""

from enum import Enum
from pathlib import Path


class _SourceRead(Enum):
    """What one per-pid liveness source actually yielded, judged on RESOLUTION.

    ``SKIP`` is the ordinary multi-user case — another uid's pid this one may not
    read at all. ``BLIND`` is the dangerous one a boolean witness cannot express:
    entries are there and NOT ONE resolves, so the source describes a world this
    process cannot see rather than an empty one.
    """

    ANSWERED = "answered"
    SKIP = "skip"
    BLIND = "blind"


# The two 0500 sources behind ``ptrace_may_access`` — the only ones whose success
# proves this process can actually see into the table it was asked to read.
_GATED_SOURCES = ("fd", "map_files")


def held_paths(proc_root: Path) -> frozenset[str] | None:
    """Every path a live process holds open under *proc_root*; ``None`` when blind.

    Four distinct liveness forms, every one of them read PER PID and folded into
    one set: a plain open ``fd`` or ``cwd``; a memory-mapped file (``map_files``)
    — a process that ``mmap()``s a file and then closes the original fd holds it
    live with NO entry under ``fd/`` at all, so skipping ``map_files`` misses
    exactly that case; and a bound ``AF_UNIX`` socket, which is not reachable
    through any fd walk (a socket fd reads back as ``socket:[inode]``, never the
    bind path) and is instead read from that pid's own ``net/unix`` table.

    A pid whose sources are unreadable belongs to another uid and is skipped
    rather than failing the whole probe — that is the normal state of any
    multi-user process table, and the caller's ownership guard already refuses to
    consider an entry this uid does not own. But when pids exist and NOT ONE of
    them answers through ANY ACCESS-GATED source, that is not "a quiet,
    single-user box" — it is the probe itself structurally blind (a container
    without ptrace/LSM access into the host process table it was bind-mounted to
    read). The socket table is deliberately NOT one of those witnesses:
    ``<pid>/net`` is mode 0555 and ``<pid>/net/unix`` 0444, so it answers for
    every pid whatever this uid may reach, while ``fd`` and ``map_files`` are
    0500 behind ``ptrace_may_access``. Letting a source that always answers vouch
    for the probe would retire this guard on any real ``/proc``, precisely where
    it is load-bearing. An unlistable ``proc_root`` blinds the WHOLE probe
    outright: it is the one process-table-wide read left, so a failure there is a
    namespace problem, not a single pid's.

    A zero-numeric-pid table blinds for the same reason: a live procfs always
    carries at least pid 1, so an empty one is a mount that is not a process
    table rather than a box with no processes.

    Blindness is PROBE-WIDE, never per-pid: an unknowable pid may hold ANY
    candidate path, so a sibling that answers buys no partial knowledge of what
    the blind one holds.
    """
    try:
        pids = [entry for entry in proc_root.iterdir() if entry.name.isdigit()]
    except OSError:
        return None
    held: set[str] = set()
    any_pid_answered = False
    for base in pids:
        for source in _GATED_SOURCES:
            resolved, read = _resolved_links(base / source)
            if read is _SourceRead.BLIND:
                return None
            if read is _SourceRead.ANSWERED:
                any_pid_answered = True
                held.update(resolved)
        held.update(_bound_socket_paths(base))
        try:
            held.add(str((base / "cwd").readlink()))
            any_pid_answered = True
        except OSError:
            continue
    if not any_pid_answered:
        return None
    return frozenset(held)


def _resolved_links(path: Path) -> tuple[frozenset[str], _SourceRead]:
    """The targets *path*'s entries resolve to, and whether the read produced EVIDENCE.

    An empty-but-readable directory IS an answer contributing nothing — every
    kernel thread presents one, so scoring it a non-answer would blind the probe
    on any real ``/proc`` and leave the sweep permanently inert.
    """
    try:
        entries = list(path.iterdir())
    except OSError:
        return frozenset(), _SourceRead.SKIP
    resolved: set[str] = set()
    for link in entries:
        try:
            resolved.add(str(link.readlink()))
        except OSError:
            continue
    if entries and not resolved:
        return frozenset(), _SourceRead.BLIND
    return frozenset(resolved), _SourceRead.ANSWERED


def _bound_socket_paths(pid_dir: Path) -> frozenset[str]:
    """Paths bound by a live ``AF_UNIX`` socket in *pid_dir*'s own network namespace.

    Always read through a NUMERIC pid dir, never the bare ``proc_root/net/unix``:
    ``/proc/net`` is a magic symlink to ``self/net``, resolved against the READING
    process, so even when ``proc_root`` is a bind mount of the host's ``/proc``
    that read SUCCEEDS and silently hands back this container's own (empty) socket
    table instead of the host's — no exception, so no fail-closed path ever fires
    on it. ``<pid>/net`` is a real directory naming that pid's namespace, the same
    handle ``nsenter --net=/proc/<pid>/ns/net`` enters a foreign one by.

    An unreadable table is a per-pid gap (a pid that exited mid-walk is the common
    one), not a probe-wide blind: pooled across every pid, one namespace's absence
    cannot be told from a genuinely socket-less one, and the probe-wide fail-closed
    contract is carried by the access-gated witness in ``held_paths`` instead.

    Each line carries the bind path (when the socket has one, rather than being
    abstract-namespaced or anonymous) as the LAST whitespace-separated field. An
    abstract-namespace name starts with ``@`` and is not a filesystem path, so it
    is excluded.
    """
    try:
        text = (pid_dir / "net" / "unix").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()
    paths: set[str] = set()
    for line in text.splitlines()[1:]:  # header: "Num RefCount Protocol Flags Type St Inode Path"
        fields = line.split(None, 7)
        if len(fields) < 8:  # noqa: PLR2004 — the fixed /proc/net/unix column count
            continue
        candidate = fields[7]
        if candidate.startswith("/"):
            paths.add(candidate)
    return frozenset(paths)


__all__ = ["held_paths"]
