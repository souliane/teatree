"""Agent-scratch retention sweep over a temp root (#4165).

A RAM-backed ``/tmp`` turns week-old agent scratch into permanent memory loss:
the measured box held 8.8 GB of stale sqlite/venv scratch inside a 15 GB tmpfs
on 31 GB of RAM, so 28% of the working pool was garbage. The pre-existing
resource-pressure ladder reached only ``/tmp/claude-statusline``, which is why
it reported ``reclaimed=0.00GB`` with that 8.8 GB sitting in front of it.

FAIL-CLOSED on every guard it cannot evaluate. A top-level entry under the root
is removable only when ALL of: older than the retention window, owned by this
uid, not held open (fd or cwd) by a live process the probe can see, not a
registered worktree checkout nor a parent/child of one, and not protected by
name. A guard that cannot be answered keeps the entry with its reason recorded,
so a read failure is never laundered into a deletion.

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


def _tree_bytes(path: Path) -> int:
    """Apparent size of *path* (file, symlink, or directory tree); 0 when unreadable."""
    try:
        if path.is_symlink() or not path.is_dir():
            return path.lstat().st_size
    except OSError:
        return 0
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                continue
    return total


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
        held = self._held_paths()
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
        """Re-plan against live state, then remove only what that fresh plan cleared."""
        plan = self.plan()
        cutoff = self._cutoff()
        reclaimed = 0
        for entry in plan.candidates:
            path = Path(entry.path)
            # Re-read the mtime immediately before the unlink: the box provisions
            # continuously, so an entry touched since the plan is live work.
            if self._age_days(path, cutoff=cutoff) is None:
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
        entries = [
            ScratchEntry(
                path=str(child),
                size_bytes=_tree_bytes(child),
                age_days=self._age_days(child, cutoff=None) or 0.0,
                removable=False,
                reason=gap,
            )
            for child in self._children()
        ]
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
        keep = self._keep_reason(child, cutoff=cutoff, held=held, reserved=reserved)
        return ScratchEntry(
            path=str(child),
            size_bytes=_tree_bytes(child),
            age_days=self._age_days(child, cutoff=None) or 0.0,
            removable=not keep,
            reason=keep or f"stale scratch older than {self.retention_days}d",
        )

    def _keep_reason(
        self,
        child: Path,
        *,
        cutoff: float,
        held: frozenset[str],
        reserved: frozenset[str],
    ) -> str:
        """Why *child* survives, or ``""`` when every guard cleared it."""
        if child.name in _PROTECTED_NAMES or child.name.startswith(_PROTECTED_PREFIXES):
            return "protected path"
        stale = self._staleness_reason(child, cutoff=cutoff)
        if stale:
            return stale
        if _covers(held, self._probe_path(child), nested_only=True):
            return "open by a live process"
        if _covers(reserved, str(child), nested_only=False):
            return "holds a tracked worktree"
        return ""

    def _staleness_reason(self, child: Path, *, cutoff: float) -> str:
        """Why *child* is not provably stale scratch of this uid, or ``""``."""
        try:
            info = child.lstat()
        except OSError:
            return "unreadable — cannot prove it is stale"
        owner = self.uid if self.uid is not None else os.getuid()
        if info.st_uid != owner:
            return f"owned by uid {info.st_uid}, not this one"
        if info.st_mtime >= cutoff:
            return f"younger than {self.retention_days}d"
        return ""

    def _probe_path(self, child: Path) -> str:
        """*child*'s path as the process table being read spells it."""
        return str((self.probe_root or self.root) / child.name)

    def _age_days(self, path: Path, *, cutoff: float | None) -> float | None:
        """Age in days; ``None`` when unreadable or (given *cutoff*) not yet stale."""
        try:
            mtime = path.lstat().st_mtime
        except OSError:
            return None
        if cutoff is not None and mtime >= cutoff:
            return None
        return (self._clock().timestamp() - mtime) / _SECONDS_PER_DAY

    def _held_paths(self) -> frozenset[str] | None:
        """Every path a live process holds as an fd or cwd; ``None`` when unreadable.

        A pid whose ``fd`` dir is unreadable belongs to another uid and is skipped
        rather than failing the whole probe — that is the normal state of any
        multi-user process table, and the ownership guard already refuses to
        consider an entry this uid does not own. Only an unlistable ``proc_root``
        (the wrong namespace, or no mount at all) blinds the probe.
        """
        try:
            pids = [entry for entry in self.proc_root.iterdir() if entry.name.isdigit()]
        except OSError:
            return None
        held: set[str] = set()
        for base in pids:
            for link in (base / "cwd", *_fd_links(base)):
                try:
                    held.add(str(link.readlink()))
                except OSError:
                    continue
        return frozenset(held)


def _ranked(entries: list[ScratchEntry]) -> tuple[ScratchEntry, ...]:
    return tuple(sorted(entries, key=lambda entry: (-entry.size_bytes, entry.path)))


def _fd_links(base: Path) -> list[Path]:
    try:
        return list((base / "fd").iterdir())
    except OSError:
        return []


def _worktree_paths() -> frozenset[str] | None:
    """Registered checkout + repo paths; ``None`` when the read fails."""
    from teatree.core.models.worktree import Worktree  # noqa: PLC0415 — lazy ORM import

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
    reading another's process table is exactly what blinds the open-file guard.
    So the container reaches the host's scratch only when BOTH are present, and an
    explicitly configured root that is not that host mount is swept against this
    venue's own ``/proc``. ``retention_days`` is left at 0 for the caller to set
    from config — an unset window is the off switch, never a default deletion.
    """
    host_view = _HOST_TMP.is_dir() and _HOST_PROC.is_dir()
    root = Path(configured) if configured else (_HOST_TMP if host_view else _VENUE_TMP)
    if host_view and root == _HOST_TMP:
        return ScratchSweep(root=_HOST_TMP, retention_days=0, probe_root=_VENUE_TMP, proc_root=_HOST_PROC)
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
