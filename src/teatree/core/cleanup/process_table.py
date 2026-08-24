"""The one process table every "is anything working in here?" guard reads (#4244).

A reaper that deletes what no process is using needs to see the processes that
matter, and the venue decides whether it can. The loop tick runs inside
``teatree-worker``, which has its own PID namespace: ``/proc`` there lists the
container's own handful of processes and none of the host agents whose working
directory is the very checkout under consideration. A guard reading ``/proc``
from there answers "nobody is inside" for every directory on the box — the
fail-OPEN shape recorded on #4306, where the guard was verified on the host and
had zero protection in the venue it actually ran in.

So the host's table is bind-mounted read-only at :data:`_HOST_PROC_ROOT`, and a
containerised caller that cannot reach it gets :attr:`ProcessTable.usable`
``False`` rather than an empty answer. What a consumer does with that is its own
decision, and the two consumers here differ on purpose: the venv reaper refuses
outright, because absence of a live process is the whole of its authority to
delete; :mod:`teatree.core.cleanup.cleanup_liveness` carries several independent
guards and its CWD signal only ever widens them.

**Read the links, never resolve them.** ``/proc/<pid>/cwd`` under a host bind
mount is a symlink into the HOST's namespace, so resolving it against the
container's root answers about a path that need not exist here — measurably "0
resolved" for a guard that is working fine. Every path below is the raw
``readlink`` string, compared as a path and never resolved.

**The QUERY is the opposite, and the asymmetry is the point.** A kernel
``readlink`` is already canonical in the namespace that produced it, so a caller
asking about a SYMLINKED spelling of that same directory never matches it — and
:func:`~teatree.core.cleanup.checkout_registry.one_spelling_each` hands the
reapers whichever spelling sorts first, which is routinely the link. Measured
with a live process inside: the real spelling read as held, the symlinked
spelling read as free, so a checkout with an agent working in it was queued for
deletion. :meth:`ProcessTable.holds` therefore matches the query under both its
raw and its resolved spelling. That can only ever WIDEN the keep-set, so it can
never itself authorise a deletion.

An individual pid declining to answer is NOT what makes the table unusable:
``/proc/<pid>/cwd`` is readable only to the pid's own uid, so on a shared box the
root daemons never answer and a table that demanded every answer would refuse
forever. What is decisive is *no* pid answering — a table listing processes none
of which will speak is exactly the blind case, and it is reported as such.
"""

from dataclasses import dataclass
from pathlib import Path

#: The host's process table as bind-mounted into a container (deploy/docker-compose.yml).
_HOST_PROC_ROOT = Path("/host-proc")

#: This venue's own table. Authoritative only when the venue IS the host.
_OWN_PROC_ROOT = Path("/proc")

#: Present iff this process runs inside a container, so ``/proc`` shows a
#: namespace that is not the host's.
_CONTAINER_MARKERS = (Path("/.dockerenv"), Path("/run/.containerenv"))

#: The per-process links that place a process inside a directory: where it is
#: working, and the binary it is running.
_PLACEMENT_LINKS = ("cwd", "exe")


@dataclass(frozen=True, slots=True)
class ProcessTable:
    """Where live processes are placed on disk, and whether that answer can be trusted."""

    paths: frozenset[Path]
    source: str
    gaps: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        """True iff the table was read from a source covering the host's processes."""
        return bool(self.source)

    def refuse_reason(self) -> str:
        """Why this table may not authorise a deletion, or ``""`` when it may.

        The one phrasing both destructive consumers refuse with, so a refusal
        reads the same wherever it surfaces.
        """
        if self.usable:
            return ""
        return "; ".join(self.gaps) or "the process table could not be read"

    def holds(self, directory: Path) -> bool:
        """True iff a live process is working in *directory* or running from inside it."""
        return any(
            path == spelling or spelling in path.parents
            for spelling in _query_spellings(directory)
            for path in self.paths
        )


def _query_spellings(directory: Path) -> tuple[Path, ...]:
    """*directory* as written and as resolved — an unresolvable path stays as written."""
    try:
        resolved = directory.resolve()
    except OSError:
        return (directory,)
    return (directory,) if resolved == directory else (directory, resolved)


def running_in_a_container() -> bool:
    """True iff this process's ``/proc`` is a container namespace, not the host's."""
    return any(marker.exists() for marker in _CONTAINER_MARKERS)


def _pid_dirs(root: Path) -> list[Path]:
    try:
        return [entry for entry in root.iterdir() if entry.name.isdigit()]
    except OSError:
        return []


def host_proc_root() -> tuple[Path | None, str]:
    """The table to read and why — ``(None, reason)`` when no source covers the host."""
    if _pid_dirs(_HOST_PROC_ROOT):
        return _HOST_PROC_ROOT, ""
    if running_in_a_container():
        return None, (
            f"containerised with no host process table at {_HOST_PROC_ROOT} — "
            f"{_OWN_PROC_ROOT} lists this container's namespace only, so a process working "
            "in a checkout on the host would read as absent"
        )
    if _pid_dirs(_OWN_PROC_ROOT):
        return _OWN_PROC_ROOT, ""
    return None, f"no readable process table at {_OWN_PROC_ROOT}"


def read_process_table() -> ProcessTable:
    """Read the host-covering process table, or report why there is none."""
    root, refusal = host_proc_root()
    if root is None:
        return ProcessTable(frozenset(), "", (refusal,))
    pids = _pid_dirs(root)
    paths: set[Path] = set()
    silent = 0
    for pid in pids:
        placements = _placements(pid)
        if not placements:
            silent += 1
            continue
        paths.update(placements)
    if pids and not paths:
        return ProcessTable(
            frozenset(),
            "",
            (f"{root} lists {len(pids)} process(es) and none would say where it is running",),
        )
    gaps = (f"{silent} of {len(pids)} process(es) under {root} did not say where they run",) if silent else ()
    return ProcessTable(frozenset(paths), str(root), gaps)


def _placements(pid_dir: Path) -> set[Path]:
    placements: set[Path] = set()
    for link in _PLACEMENT_LINKS:
        try:
            placements.add((pid_dir / link).readlink())
        except OSError:
            continue
    return placements


__all__ = ["ProcessTable", "host_proc_root", "read_process_table", "running_in_a_container"]
