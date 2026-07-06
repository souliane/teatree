"""Relocate an overlay's teatree-managed worktrees to the per-overlay dir.

The engine behind ``t3 <overlay> workspace relocate`` (regroup worktrees under
``~/workspace/t3-workspaces/<overlay>/``). It moves each ``Worktree`` whose
on-disk path is NOT already under the resolved per-overlay ``target_root`` with
``git worktree move`` (NEVER a raw ``mv`` — git's worktree admin must update so
the moved worktree stays linked to its clone), then rewrites the row's stored
``extra['worktree_path']``.

Safety doctrine — a worktree is SKIPPED (never moved) and reported when it is:

* **git-locked** (``git worktree lock``) — moving a locked worktree is refused;
* **dirty** (uncommitted changes) — a live mid-task worktree's edits must not ride a move;
* **active** — a live mid-task worktree: its ticket has a live session/active task, or the CWD is inside it.

It is **idempotent** (a worktree already under ``target_root`` is a no-op),
supports ``--dry-run`` (plan the moves, touch nothing), and **continues past a
single failed move** (reports it, never aborts the run).
"""

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from django.db import DatabaseError

from teatree.config import OverlayEntry
from teatree.core.models import Worktree
from teatree.core.worktree.clone_paths import find_clone_path
from teatree.utils import git
from teatree.utils.run import CommandFailedError


@dataclass(frozen=True)
class RelocateIO:
    """The command's output sinks (``self.stdout.write`` / ``self.stderr.write``)."""

    write_out: Callable[[str], None]
    write_err: Callable[[str], None]


@dataclass
class RelocateResult:
    """Per-disposition tallies of a relocate run, rendered for the CLI return."""

    moved: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    dry_run: bool = False

    def render(self) -> list[str]:
        verb = "would move" if self.dry_run else "moved"
        lines = [f"{verb} {line}" for line in self.moved]
        lines += [f"skipped {line}" for line in self.skipped]
        lines += [f"FAILED {line}" for line in self.failed]
        if not lines:
            lines.append("no teatree-managed worktrees to relocate")
        return lines


def _ticket_is_busy(worktree: Worktree) -> bool:
    """True iff the worktree's ticket has a live session or an active/claimed task."""
    return worktree.ticket.has_active_work()


def _is_active_cwd(old_resolved: Path, active_path: Path | None) -> bool:
    """True iff *active_path* is the worktree's own dir or a child of it."""
    if active_path is None:
        return False
    return active_path == old_resolved or old_resolved in active_path.parents


def _active_cwd() -> Path | None:
    # Residual gap (acknowledged): this only sees THIS process's cwd, so a
    # concurrent agent process whose cwd is inside the worktree is not caught
    # here — the session/task liveness checks (``_ticket_is_busy``) cover that
    # real agent case, so a live mid-task worktree is still skipped.
    try:
        return Path.cwd().resolve()
    except OSError:
        return None


@dataclass(frozen=True)
class _Candidate:
    """One worktree row resolved for relocation: the row, its on-disk paths, its clone."""

    worktree: Worktree
    old: Path
    old_resolved: Path
    clone: str | None


def _resolve_clone(worktree: Worktree, old: Path) -> str | None:
    """The source clone ``git worktree move`` runs from (NOT *old* itself).

    Prefers the provision-time ``extra['clone_path']``; falls back to a scan of
    the OLD workspace root (``<old_ws>/<branch>/<repo>`` → ``<old_ws>`` is
    ``old.parent.parent``). ``None`` when no clone can be located.
    """
    stored = (worktree.extra or {}).get("clone_path")
    if stored:
        return str(stored)
    found = find_clone_path(old.parent.parent, worktree.repo_path)
    return str(found) if found is not None else None


def _dirty_reason(old: Path) -> str | None:
    """The skip reason for a dirty / undeterminable worktree, or ``None`` when clean.

    Fail-closed: a ``git status`` error keeps the worktree (treated as "might be
    dirty") so a flaky probe can't strand a live edit.
    """
    try:
        dirty = bool(git.status_porcelain_strict(str(old)).strip())
    except CommandFailedError:
        return "could not determine git status (kept)"
    return "uncommitted changes" if dirty else None


def _skip_reason(candidate: _Candidate, target_root_resolved: Path, *, active_path: Path | None) -> str | None:
    """The reason this worktree must NOT be moved, or ``None`` when it is movable.

    Ordered cheapest-and-most-decisive first: idempotent-skip, missing source
    clone, active CWD, busy ticket, git-lock, then the dirty probe.
    """
    old_resolved = candidate.old_resolved
    if target_root_resolved == old_resolved or target_root_resolved in old_resolved.parents:
        return f"already under {target_root_resolved}"
    if candidate.clone is None:
        return "source clone not found"
    if _is_active_cwd(old_resolved, active_path):
        return "active worktree (current working directory)"
    if _ticket_is_busy(candidate.worktree):
        return "ticket has a live session or active/claimed task"
    if old_resolved in {Path(p) for p in git.locked_worktree_paths(candidate.clone)}:
        return "git-locked"
    return _dirty_reason(candidate.old)


def _matches_overlay(worktree_overlay: str, overlay_name: str) -> bool:
    """Canonical-alias-tolerant overlay match (``teatree`` ≡ ``t3-teatree``)."""
    return OverlayEntry.canonical_overlay_name(worktree_overlay) == OverlayEntry.canonical_overlay_name(overlay_name)


def active_overlay_name() -> str:
    """The active overlay name, resolved exactly as ``config.worktree_root()`` does.

    ``T3_OVERLAY_NAME`` → cwd discovery → the single installed overlay, so the
    relocate scope and the per-overlay ``target_root`` always agree on the overlay.
    """
    from teatree.config.resolution import _resolved_overlay_name  # noqa: PLC0415

    return _resolved_overlay_name(None)


def run_relocate(overlay_name: str, target_root: Path, io: RelocateIO, *, dry_run: bool) -> RelocateResult:
    """Relocate *overlay_name*'s teatree-managed worktrees under *target_root*.

    *target_root* is the resolved per-overlay WORKTREE root
    (``config.worktree_root()``). Each movable worktree is moved with
    ``git worktree move`` and its row's ``extra['worktree_path']`` rewritten;
    locked/dirty/active worktrees are skipped (reported), the run is idempotent,
    ``dry_run`` plans without touching anything, and one failed move never aborts
    the rest. A row whose recorded path is gone but whose worktree already sits
    under *target_root* (a prior run's git move succeeded then the DB save threw)
    is self-healed — see :func:`_reconcile_half_move`.
    """
    result = RelocateResult(dry_run=dry_run)
    target_root_resolved = target_root.resolve()
    active_path = _active_cwd()

    worktrees = (
        wt
        for wt in Worktree.objects.select_related("ticket").order_by("pk")
        if _matches_overlay(wt.overlay, overlay_name)
    )
    for worktree in worktrees:
        wt_path = worktree.worktree_path
        if not wt_path:
            _record_skip(result, io, f"{worktree.repo_path}: no recorded worktree path")
            continue
        old = Path(wt_path)
        if not old.exists():
            # A recorded path gone from disk is normally a stale row — but if the
            # worktree already sits under target_root, a prior run's git move
            # succeeded then its DB save failed (the #regroup half-move). Heal the
            # row instead of skipping it forever.
            target = _half_move_target(old, target_root_resolved)
            if target is None:
                _record_skip(result, io, f"{old}: worktree path missing on disk (stale row)")
            else:
                _reconcile_half_move(result, io, worktree, target, dry_run=dry_run)
            continue
        candidate = _Candidate(
            worktree=worktree, old=old, old_resolved=old.resolve(), clone=_resolve_clone(worktree, old)
        )
        reason = _skip_reason(candidate, target_root_resolved, active_path=active_path)
        if reason is not None:
            _record_skip(result, io, f"{old}: {reason}")
            continue

        target = target_root_resolved / candidate.old_resolved.parent.name / candidate.old_resolved.name
        line = f"{old} -> {target}"
        if dry_run:
            result.moved.append(line)
            io.write_out(f"  would move {line}")
            continue
        _move_one(result, io, candidate, target, line)

    return result


def _move_one(result: RelocateResult, io: RelocateIO, candidate: _Candidate, target: Path, line: str) -> None:
    """Execute one ``git worktree move`` + DB-row rewrite, reporting success/failure."""
    with suppress(OSError):
        target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # candidate.clone is non-None here: _skip_reason returned "source clone not found" otherwise.
        git.worktree_move(str(candidate.clone), str(candidate.old), str(target))
    except CommandFailedError as exc:
        result.failed.append(f"{line}: {exc}")
        io.write_err(f"  FAILED {candidate.old}: {exc}")
        return
    worktree = candidate.worktree
    extra = dict(worktree.extra or {})
    extra["worktree_path"] = str(target)
    worktree.extra = extra
    try:
        worktree.save(update_fields=["extra"])
    except DatabaseError as exc:
        # Git + disk are NOW at `target`, but the row save failed, so it still
        # records the OLD (now-gone) path. Report it (never silently lost, never
        # aborts the run); a subsequent run's reconcile step (recorded path gone +
        # a worktree present under the target root) self-heals the row.
        msg = f"moved on disk but DB row not updated ({exc}); re-run to reconcile"
        result.failed.append(f"{line}: {msg}")
        io.write_err(f"  FAILED {candidate.old}: {msg}")
        return
    result.moved.append(line)
    io.write_out(f"  moved {line}")


def _half_move_target(old: Path, target_root_resolved: Path) -> Path | None:
    """The moved location of a half-moved worktree, or ``None`` when there is none.

    A worktree row records ``<old_ws>/<branch>/<repo>``; its post-move home is
    ``<target_root>/<branch>/<repo>``. When ``old`` is gone from disk but that
    target exists AS a git worktree (a ``.git`` entry), a prior run moved it on
    disk + git but failed to save the row — return the target so the caller heals
    the row. Pure check: no DB write, no filesystem mutation.
    """
    target = target_root_resolved / old.parent.name / old.name
    return target if (target / ".git").exists() else None


def _reconcile_half_move(
    result: RelocateResult, io: RelocateIO, worktree: Worktree, target: Path, *, dry_run: bool
) -> None:
    """Heal a #regroup half-move: re-point the stale row at its already-moved worktree.

    The row's recorded path is gone but the worktree already sits under
    ``target_root`` (a prior run's git move succeeded, then its DB save threw).
    Under ``dry_run`` it plans the reconcile without touching the DB. The row
    still records the OLD path, so ``worktree.worktree_path`` is the move source.
    """
    line = f"{worktree.worktree_path} -> {target}"
    if dry_run:
        result.moved.append(line)
        io.write_out(f"  would reconcile {line}")
        return
    extra = dict(worktree.extra or {})
    extra["worktree_path"] = str(target)
    worktree.extra = extra
    worktree.save(update_fields=["extra"])
    result.moved.append(line)
    io.write_out(f"  reconciled {line}")


def _record_skip(result: RelocateResult, io: RelocateIO, line: str) -> None:
    result.skipped.append(line)
    io.write_out(f"  skipped {line}")
