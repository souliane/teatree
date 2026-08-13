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

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class _SourceRead(Enum):
    """What one per-pid liveness source actually yielded, judged on RESOLUTION.

    ``UNREADABLE`` (the listing itself refused) and ``BLIND`` (entries are there
    and NOT ONE resolves) are ONE knowledge state — this process cannot see what
    that pid holds — so they share a consequence and differ only in the reason
    reported. ``GONE`` is the benign absence: an ``ENOENT`` means the pid exited
    mid-walk, which is a table that moved rather than one this uid may not read.
    """

    ANSWERED = "answered"
    GONE = "gone"
    UNREADABLE = "unreadable"
    BLIND = "blind"


# The two 0500 sources behind ``ptrace_may_access`` — the only ones whose success
# proves this process can actually see into the table it was asked to read.
_GATED_SOURCES = ("fd", "map_files")

_UNKNOWABLE_CAUSE: dict[_SourceRead, str] = {
    _SourceRead.BLIND: "listable but nothing resolves",
    _SourceRead.UNREADABLE: "not readable by this uid",
}
_REASON_PID_SAMPLE = 5


@dataclass(frozen=True, slots=True)
class ProcessTableView:
    """What one read of a process table saw, and how much of it it could NOT see.

    The consumer never has to tell empty-because-nothing-is-held from
    empty-because-we-could-not-look: ``held`` is only usable when ``sighted``,
    and ``unknowable_reason`` names why it is not.
    """

    held: frozenset[str]
    answered_pids: int
    unknowable_pids: int
    unknowable_reason: str = ""

    @property
    def sighted(self) -> bool:
        """True only when EVERY pid answered — an unknowable one may hold ANY candidate."""
        return self.answered_pids > 0 and self.unknowable_pids == 0


def normalized_spelling(path: str) -> str:
    """*path* with its PARENT resolved and its leaf left alone — one spelling to compare by.

    A held path and a sweep candidate that name the same file through different
    symlinked components compare unequal, so the guard misses and a live-held
    entry is marked reclaimable. The leaf is deliberately NOT resolved: a
    top-level entry that is itself a symlink must stay itself, because the sweep
    unlinks it without following. A non-absolute string is a kernel pseudo-target
    (``socket:[…]``, ``pipe:[…]``) rather than a path, and is left verbatim.
    """
    if not path.startswith(os.sep):
        return path
    candidate = Path(path)
    return str(Path(os.path.realpath(candidate.parent)) / candidate.name)


def held_paths(proc_root: Path) -> ProcessTableView:
    """Every path a live process holds open under *proc_root*, plus the read's own coverage.

    Four distinct liveness forms, every one of them read PER PID and folded into
    one set: a plain open ``fd`` or ``cwd``; a memory-mapped file (``map_files``)
    — a process that ``mmap()``s a file and then closes the original fd holds it
    live with NO entry under ``fd/`` at all, so skipping ``map_files`` misses
    exactly that case; and a bound ``AF_UNIX`` socket, which is not reachable
    through any fd walk (a socket fd reads back as ``socket:[inode]``, never the
    bind path) and is instead read from that pid's own ``net/unix`` table.

    A pid whose sources are UNREADABLE is counted, never dropped. The old
    contract skipped it on the strength of the caller's ownership guard, but that
    guard stats the swept ENTRY's uid and never the HOLDER's — a root pid holding
    a file inside this uid's scratch dir is exactly the uncovered case, and 214 of
    304 pids on the measured box are root-owned. So an unreadable pid is the same
    sentence as a blind one: we cannot see what it holds. When pids exist and NOT
    ONE of them answers through ANY ACCESS-GATED source, that is not "a quiet,
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

    Blindness stays PROBE-WIDE for the delete decision, never per-pid: an
    unknowable pid may hold ANY candidate path, so a sibling that answers buys no
    partial knowledge of what the blind one holds. The per-pid detail is carried
    only as the reported REASON.
    """
    try:
        pids = [entry for entry in proc_root.iterdir() if entry.name.isdigit()]
    except OSError as error:
        return _unsighted(f"process table at {proc_root} is not listable ({error.strerror or error})")
    if not pids:
        return _unsighted(f"process table at {proc_root} carries no numeric pid — not a process table")
    held: set[str] = set()
    answered = 0
    unknowable: list[tuple[str, str]] = []
    for base in pids:
        reads: list[_SourceRead] = []
        for source in _GATED_SOURCES:
            resolved, read = _resolved_links(base / source)
            reads.append(read)
            held.update(resolved)
        held.update(_bound_socket_paths(base))
        reads.append(_read_cwd(base, held))
        if _SourceRead.ANSWERED in reads:
            answered += 1
        blocked = next((read for read in (_SourceRead.BLIND, _SourceRead.UNREADABLE) if read in reads), None)
        if blocked is not None:
            unknowable.append((base.name, _UNKNOWABLE_CAUSE[blocked]))
    return ProcessTableView(
        held=frozenset(held),
        answered_pids=answered,
        unknowable_pids=len(unknowable),
        unknowable_reason=_coverage_reason(unknowable, pid_count=len(pids), answered=answered, proc_root=proc_root),
    )


def _unsighted(reason: str) -> ProcessTableView:
    return ProcessTableView(held=frozenset(), answered_pids=0, unknowable_pids=0, unknowable_reason=reason)


def _read_cwd(pid_dir: Path, held: set[str]) -> _SourceRead:
    """Resolve *pid_dir*'s ``cwd``, folding its target into *held*.

    Also ``ptrace``-gated, so a refusal here is the same blindness the ``fd``
    walk reports rather than the silent ``continue`` it used to be.
    """
    try:
        held.add(str((pid_dir / "cwd").readlink()))
    except FileNotFoundError:
        return _SourceRead.GONE
    except OSError:
        return _SourceRead.UNREADABLE
    return _SourceRead.ANSWERED


def _coverage_reason(
    unknowable: list[tuple[str, str]],
    *,
    pid_count: int,
    answered: int,
    proc_root: Path,
) -> str:
    """Counts by cause plus the first few offending pids, or ``""`` when the read was complete."""
    if not unknowable:
        if answered:
            return ""
        return f"no pid under {proc_root} answered through an access-gated source"
    tally: dict[str, int] = {}
    for _pid, cause in unknowable:
        tally[cause] = tally.get(cause, 0) + 1
    causes = ", ".join(f"{count} {cause}" for cause, count in sorted(tally.items()))
    sample = ", ".join(pid for pid, _cause in unknowable[:_REASON_PID_SAMPLE])
    return f"{len(unknowable)} of {pid_count} pid(s) unknowable ({causes}); first: {sample}"


def _resolved_links(path: Path) -> tuple[frozenset[str], _SourceRead]:
    """The targets *path*'s entries resolve to, and whether the read produced EVIDENCE.

    An empty-but-readable directory IS an answer contributing nothing — every
    kernel thread presents one, so scoring it a non-answer would blind the probe
    on any real ``/proc`` and leave the sweep permanently inert.
    """
    try:
        entries = list(path.iterdir())
    except FileNotFoundError:
        return frozenset(), _SourceRead.GONE
    except OSError:
        return frozenset(), _SourceRead.UNREADABLE
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


__all__ = ["ProcessTableView", "held_paths", "normalized_spelling"]
