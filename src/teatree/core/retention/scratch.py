"""Agent-scratch retention sweep over a temp root (#4165).

A RAM-backed ``/tmp`` turns week-old agent scratch into permanent memory loss:
the measured box held 8.8 GB of stale sqlite/venv scratch inside a 15 GB tmpfs
on 31 GB of RAM, so 28% of the working pool was garbage. The pre-existing
resource-pressure ladder reached only ``/tmp/claude-statusline``, which is why
it reported ``reclaimed=0.00GB`` with that 8.8 GB sitting in front of it.

FAIL-CLOSED on every guard it cannot evaluate. A top-level entry under the root
is removable only when ALL of: no file ANYWHERE in its tree was touched inside
the retention window (not just the top-level entry's own mtime — a directory's
own mtime moves only when an entry is added/removed DIRECTLY inside it, so a
working tree whose deep content was written seconds ago still reads as stale by
that measure alone), owned by this uid, not held open by a live process the
probe can see — as an fd, a cwd, or an mmap (``map_files``), and not the bind
path of a live AF_UNIX socket (that pid's own ``<pid>/net/unix``, invisible to
any fd walk; never the bare ``proc_root/net/unix``, which is a magic symlink to
the READING process's ``self/net`` and so answers for the wrong namespace) —
not a git repository anywhere in its tree (a registered ``Worktree`` row OR an
ad-hoc clone the DB never learned about), and not protected by name. A guard
that cannot be answered keeps the entry with its reason recorded, so a read
failure is never laundered into a deletion — including when the open-file probe
(:mod:`~teatree.core.retention.liveness`) can see a pid's ``fd`` directory but
resolve nothing inside it: that is not an empty process table, it is the probe
itself unable to see the namespace it was asked to watch, and it blinds the
whole sweep rather than reporting nothing held.

The process table and the temp root must describe the SAME namespace or the
open-file guard is blind: ``root`` is what this venue writes through, and
``probe_root`` is that same directory's path in the namespace ``proc_root``
describes. The containerised deployment sweeps the host's ``/tmp`` through
``/host-tmp`` while reading the host process table through ``/host-proc``. When
no process table can be read at all the plan carries ``probe_gap`` and nothing
is removable — a sweep that cannot see holders removes nothing rather than
guessing.
"""

import logging
import os
import shutil
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from django.utils import timezone

from teatree.core.models.worktree import Worktree
from teatree.core.retention.liveness import held_paths

logger = logging.getLogger(__name__)

_SECONDS_PER_DAY = 86400
_BYTES_PER_GIB = 1024**3


# Never swept at any age. The X11/ICE sockets and the deploy flock are live
# rendezvous points whose absence breaks a running process; the statusline dir
# is aged by the resource ladder's own 2-day rule, and two owners of one path
# would race.
_PROTECTED_NAMES: frozenset[str] = frozenset(
    {
        ".ICE-unix",
        ".Test-unix",
        ".X11-unix",
        ".XIM-unix",
        ".font-unix",
        "claude-statusline",
        "teatree-deploy.lock",
    }
)
_PROTECTED_PREFIXES: tuple[str, ...] = ("systemd-private-", ".teatree-")

_HOST_TMP = Path("/host-tmp")
_HOST_PROC = Path("/host-proc")
_VENUE_TMP = Path("/tmp")  # noqa: S108 — auditing the temp root, not creating a temp file
_VENUE_PROC = Path("/proc")

# The same variable the compose mount source reads (``${TEATREE_HOST_TMP:-/tmp}``),
# forwarded into the container's own environment so this process can read the value
# too. The open-file guard compares candidate paths against what the HOST process
# table spells them as, so probe_root must name the host's real temp path — reading
# anything else (a hard-coded ``/tmp``) reconstructs the exact bug this variable
# fixes whenever the operator overrides the mount source.
_HOST_TMP_ENV = "TEATREE_HOST_TMP"


@dataclass(frozen=True, slots=True)
class ScratchEntry:
    """One top-level entry under the swept root, with the verdict that decided it."""

    path: str
    size_bytes: int
    age_days: float
    removable: bool
    reason: str

    @property
    def size_human(self) -> str:
        """Binary units — these are filesystem bytes, not docker's SI-reported ones."""
        size = float(self.size_bytes)
        for unit in ("B", "KiB", "MiB"):
            if size < 1024:  # noqa: PLR2004 — the unit step itself
                return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}GiB"


@dataclass(frozen=True, slots=True)
class ScratchSweepPlan:
    """What a sweep would reclaim (or did), size-ranked, with every keep explained."""

    root: str
    retention_days: int
    entries: tuple[ScratchEntry, ...]
    probe_gap: str = ""
    applied: bool = False
    reclaimed_bytes: int = 0

    @property
    def candidates(self) -> tuple[ScratchEntry, ...]:
        return tuple(entry for entry in self.entries if entry.removable)

    @property
    def candidate_bytes(self) -> int:
        return sum(entry.size_bytes for entry in self.candidates)

    @property
    def resident_bytes(self) -> int:
        return sum(entry.size_bytes for entry in self.entries)

    @property
    def summary(self) -> str:
        verb = "reclaimed" if self.applied else "would reclaim"
        freed = self.reclaimed_bytes if self.applied else self.candidate_bytes
        head = (
            f"{self.root}: {verb} {freed / _BYTES_PER_GIB:.2f} GB of "
            f"{self.resident_bytes / _BYTES_PER_GIB:.2f} GB resident "
            f"across {len(self.candidates)}/{len(self.entries)} entry(ies)"
        )
        return f"{head} — {self.probe_gap}" if self.probe_gap else head


@dataclass(frozen=True, slots=True)
class _TreeStats:
    """What one recursive walk of a candidate tree answers, computed together.

    ``newest_mtime`` is the NEWEST ``st_mtime`` of any node anywhere in the
    tree (every file's own mtime, every directory's own mtime, and the top
    entry's), never just the top-level entry's own stat. A directory's own
    mtime moves only when an entry is added, removed, or renamed DIRECTLY
    inside it — editing a file two levels down never touches it — so a
    top-level-only check reads an actively-written working tree as stale the
    moment its own last direct-child change aged out of the window, even
    though content underneath was written seconds ago.

    ``holds_git_repo`` is True when a ``.git`` entry (directory for a clone,
    file for a worktree) exists anywhere in the tree — the ad-hoc-repo guard
    that catches a git checkout the ``Worktree`` table never learned about.
    ``None`` for ``newest_mtime`` means the top entry itself could not be
    stat-ed, OR some directory anywhere in the tree could not be SCANNED (an
    ``EACCES`` on a subdirectory this uid cannot search) — either way never
    removable, because an unscanned subtree could hold content written a
    moment ago and silently under-reporting it is the same fail-open shape
    as reading only the top-level entry's own mtime.
    """

    size_bytes: int
    newest_mtime: float | None
    holds_git_repo: bool


def _tree_stats(path: Path) -> _TreeStats:
    """Size, tree-wide newest mtime, and ad-hoc-repo detection in ONE walk."""
    try:
        top = path.lstat()
    except OSError:
        return _TreeStats(size_bytes=0, newest_mtime=None, holds_git_repo=False)
    if path.is_symlink() or not path.is_dir():
        return _TreeStats(size_bytes=top.st_size, newest_mtime=top.st_mtime, holds_git_repo=False)
    total_size = 0
    newest = top.st_mtime
    try:
        # A directory this uid cannot search (EACCES) raises here, same as it
        # will on os.walk's own scandir below — caught there via `unreadable`.
        holds_git = (path / ".git").exists()
    except OSError:
        holds_git = False
    unreadable = False

    def _blind(_error: OSError) -> None:
        # os.walk's default onerror=None silently drops whatever a subtree it
        # cannot scandir would have contributed — the same fail-open shape as
        # reading only the top-level mtime, one level deeper. Recorded, not
        # swallowed: newest_mtime becomes None below, so the entry is kept.
        nonlocal unreadable
        unreadable = True

    for root, dirs, files in os.walk(path, onerror=_blind, followlinks=False):
        if ".git" in dirs or ".git" in files:
            holds_git = True
        file_stats, files_denied = _lstat_all(root, files)
        dir_stats, dirs_denied = _lstat_all(root, dirs)
        unreadable = unreadable or files_denied or dirs_denied
        for info in file_stats:
            total_size += info.st_size
            newest = max(newest, info.st_mtime)
        for info in dir_stats:
            newest = max(newest, info.st_mtime)
    return _TreeStats(
        size_bytes=total_size,
        newest_mtime=None if unreadable else newest,
        holds_git_repo=holds_git,
    )


def _lstat_all(root: str, names: list[str]) -> tuple[list[os.stat_result], bool]:
    """``lstat()`` each of *names* under *root*, plus whether a read was DENIED rather than absent.

    A directory readable but not searchable (0444) lets ``os.walk``'s scandir
    succeed — so ``onerror`` never fires — while every child ``lstat`` raises
    EACCES. Swallowing that alongside the vanished-entry case reports the tree's
    newest mtime as the parent's, so content written seconds ago classifies as
    stale; a denial is a blind spot, not an absence.
    """
    stats = []
    denied = False
    for name in names:
        try:
            stats.append((Path(root) / name).lstat())
        except PermissionError:
            denied = True
        except OSError:
            continue
    return stats, denied


def _remove(path: Path) -> bool:
    """Delete *path* (tree or single node) without following symlinks; False on failure."""
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError:
        logger.warning("scratch sweep: could not remove %s", path)
        return False
    return True


@dataclass(frozen=True, slots=True)
class ScratchSweep:
    """Plan and apply agent-scratch retention over one temp root.

    ``retention_days <= 0`` is the documented off switch: every entry is kept and
    the plan says which switch stopped it, rather than reporting a silent no-op.
    """

    root: Path
    retention_days: int
    probe_root: Path | None = None
    proc_root: Path = Path("/proc")
    uid: int | None = None
    now: datetime | None = None

    def plan(self) -> ScratchSweepPlan:
        """Classify every top-level entry under the root, size-ranked, touching nothing."""
        if self.retention_days <= 0:
            return self._inert_plan(f"retention disabled (scratch_retention_days={self.retention_days})")
        held = held_paths(self.proc_root)  # the only liveness probe; apply() never re-runs it (#4356)
        if held is None:
            return self._inert_plan(f"open-file probe unreadable at {self.proc_root} — nothing is removable")
        reserved = _worktree_paths()
        if reserved is None:
            return self._inert_plan("registered-worktree read failed — nothing is removable")
        cutoff = self._cutoff()
        entries = [self._classify(child, cutoff=cutoff, held=held, reserved=reserved) for child in self._children()]
        return ScratchSweepPlan(
            root=str(self.root),
            retention_days=self.retention_days,
            entries=_ranked(entries),
        )

    def apply(self) -> ScratchSweepPlan:
        """Re-plan against live state, then remove only what that fresh plan cleared.

        STALENESS is rechecked per entry below; LIVENESS is not — it is probed once,
        in ``plan()``. A process that opens a stale path between the plan and the
        unlink is invisible, and the mtime recheck cannot see a read-open. Left open
        deliberately (#4356): a second probe only narrows the window while looking
        like a close, and the real fixes — a lease over the root, or per-entry
        openat+flock — are a design decision. Unreachable while the lane ships off.
        """
        plan = self.plan()
        cutoff = self._cutoff()
        reclaimed = 0
        for entry in plan.candidates:
            path = Path(entry.path)
            # Re-walk the WHOLE tree immediately before the unlink, not just the
            # top-level entry: the box provisions continuously, and a nested file
            # written since the plan is live work the top-level stat alone would
            # miss (the exact gap tree-wide staleness exists to close).
            stats = _tree_stats(path)
            if stats.newest_mtime is None or stats.newest_mtime >= cutoff:
                continue
            if _remove(path):
                reclaimed += entry.size_bytes
        logger.info("scratch sweep: reclaimed %.2f GB under %s", reclaimed / _BYTES_PER_GIB, self.root)
        return ScratchSweepPlan(
            root=plan.root,
            retention_days=plan.retention_days,
            entries=plan.entries,
            probe_gap=plan.probe_gap,
            applied=True,
            reclaimed_bytes=reclaimed,
        )

    def _inert_plan(self, gap: str) -> ScratchSweepPlan:
        """A plan that removes nothing but still reports what is resident and why."""
        entries = []
        for child in self._children():
            stats = _tree_stats(child)
            entries.append(
                ScratchEntry(
                    path=str(child),
                    size_bytes=stats.size_bytes,
                    age_days=self._age_days_from_mtime(stats.newest_mtime) or 0.0,
                    removable=False,
                    reason=gap,
                )
            )
        return ScratchSweepPlan(
            root=str(self.root),
            retention_days=self.retention_days,
            entries=_ranked(entries),
            probe_gap=gap,
        )

    def _children(self) -> list[Path]:
        try:
            return sorted(self.root.iterdir())
        except OSError:
            logger.warning("scratch sweep: root %s is not listable", self.root)
            return []

    def _cutoff(self) -> float:
        return self._clock().timestamp() - self.retention_days * _SECONDS_PER_DAY

    def _clock(self) -> datetime:
        return self.now or timezone.now()

    def _classify(
        self,
        child: Path,
        *,
        cutoff: float,
        held: frozenset[str],
        reserved: frozenset[str],
    ) -> ScratchEntry:
        stats = _tree_stats(child)
        keep = self._keep_reason(child, stats=stats, cutoff=cutoff, held=held, reserved=reserved)
        return ScratchEntry(
            path=str(child),
            size_bytes=stats.size_bytes,
            age_days=self._age_days_from_mtime(stats.newest_mtime) or 0.0,
            removable=not keep,
            reason=keep or f"stale scratch older than {self.retention_days}d",
        )

    def _keep_reason(
        self,
        child: Path,
        *,
        stats: _TreeStats,
        cutoff: float,
        held: frozenset[str],
        reserved: frozenset[str],
    ) -> str:
        """Why *child* survives, or ``""`` when every guard cleared it."""
        if child.name in _PROTECTED_NAMES or child.name.startswith(_PROTECTED_PREFIXES):
            return "protected path"
        stale = self._staleness_reason(child, stats=stats, cutoff=cutoff)
        if stale:
            return stale
        if _covers(held, self._probe_path(child), nested_only=True):
            return "open by a live process"
        if _covers(reserved, str(child), nested_only=False):
            return "holds a tracked worktree"
        if stats.holds_git_repo:
            return "holds a git repository"
        return ""

    def _staleness_reason(self, child: Path, *, stats: _TreeStats, cutoff: float) -> str:
        """Why *child* is not provably stale scratch of this uid, or ``""``."""
        if stats.newest_mtime is None:
            return "unreadable — cannot prove it is stale"
        try:
            owner = child.lstat().st_uid
        except OSError:
            return "unreadable — cannot prove it is stale"
        expected = self.uid if self.uid is not None else os.getuid()
        if owner != expected:
            return f"owned by uid {owner}, not this one"
        if stats.newest_mtime >= cutoff:
            return f"younger than {self.retention_days}d"
        return ""

    def _probe_path(self, child: Path) -> str:
        """*child*'s path as the process table being read spells it."""
        return str((self.probe_root or self.root) / child.name)

    def _age_days_from_mtime(self, mtime: float | None) -> float | None:
        """Age in days of *mtime* against the sweep's clock; ``None`` when unreadable."""
        if mtime is None:
            return None
        return (self._clock().timestamp() - mtime) / _SECONDS_PER_DAY


def _ranked(entries: list[ScratchEntry]) -> tuple[ScratchEntry, ...]:
    return tuple(sorted(entries, key=lambda entry: (-entry.size_bytes, entry.path)))


def _worktree_paths() -> frozenset[str] | None:
    """Registered checkout + repo paths; ``None`` when the read fails."""
    try:
        rows = list(Worktree.objects.values_list("repo_path", "extra"))
    except Exception:
        logger.exception("scratch sweep: could not read registered worktrees")
        return None
    paths: set[str] = set()
    for repo_path, extra in rows:
        checkout = extra.get("worktree_path", "") if isinstance(extra, dict) else ""
        paths.update(str(value) for value in (repo_path, checkout) if value)
    return frozenset(paths)


def _covers(paths: frozenset[str], candidate: str, *, nested_only: bool) -> bool:
    """True when *candidate* is one of *paths*, contains one, or (unless *nested_only*) is inside one.

    A held fd names a file INSIDE the candidate, so nesting one way is enough there.
    A worktree relationship matters in both directions: the candidate may be the
    checkout, hold it, or sit inside its root.
    """
    for path in paths:
        if path == candidate or path.startswith(candidate + os.sep):
            return True
        if not nested_only and candidate.startswith(path + os.sep):
            return True
    return False


def resolve_scratch_sweep(configured: str = "") -> ScratchSweep:
    """A sweep whose root is paired with the process table that can see its holders.

    The host's temp root and its process table are mounted as a PAIR
    (``/host-tmp`` + ``/host-proc``), because sweeping one namespace's files while
    reading another's process table is exactly what blinds the open-file guard. So
    the container reaches the host's scratch only when BOTH are present, and an
    explicitly configured root that is not that host mount is swept against this
    venue's own ``/proc``. The host view's ``probe_root`` is read from
    ``TEATREE_HOST_TMP`` — the SAME variable the mount source is built from — so an
    operator override that moves the host mount source moves the guard's namespace
    with it; a stale ``/tmp`` here would blind the guard the moment the two diverge.
    ``retention_days`` is left at 0 for the caller to set from config — an unset
    window is the off switch, never a default deletion.
    """
    host_view = _HOST_TMP.is_dir() and _HOST_PROC.is_dir()
    root = Path(configured) if configured else (_HOST_TMP if host_view else _VENUE_TMP)
    if host_view and root == _HOST_TMP:
        probe_root = Path(os.environ.get(_HOST_TMP_ENV) or _VENUE_TMP)
        return ScratchSweep(root=_HOST_TMP, retention_days=0, probe_root=probe_root, proc_root=_HOST_PROC)
    return ScratchSweep(root=root, retention_days=0, proc_root=_VENUE_PROC)


def sweep_scratch(*, configured_root: str, retention_days: int, apply: bool) -> ScratchSweepPlan:
    """Plan (or apply) the scratch retention sweep for the resolved root."""
    resolved = resolve_scratch_sweep(configured_root)
    sweep = replace(resolved, retention_days=retention_days)
    return sweep.apply() if apply else sweep.plan()


__all__ = [
    "ScratchEntry",
    "ScratchSweep",
    "ScratchSweepPlan",
    "resolve_scratch_sweep",
    "sweep_scratch",
]
