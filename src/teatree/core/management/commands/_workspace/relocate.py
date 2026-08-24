"""Relocate an overlay's teatree-managed worktrees to the per-overlay dir.

The engine behind ``t3 <overlay> workspace relocate`` (regroup worktrees under
``~/workspace/t3-workspaces/<overlay>/``). It moves each ``Worktree`` whose
on-disk path is NOT already under the resolved per-overlay ``target_root`` with
``git worktree move`` (NEVER a raw ``mv`` — git's worktree admin must update so
the moved worktree stays linked to its clone), then rewrites the row's stored
``extra['worktree_path']``.

Which worktrees it refuses, and why, is :mod:`teatree.core.worktree.relocation` —
shared with the ``t3 doctor`` canonical-root check so the doctor never prescribes
this command for a worktree it would refuse.

It is **idempotent** (a worktree already under ``target_root`` is a no-op),
supports ``--dry-run`` (plan the moves, touch nothing), and **continues past a
single failed move** (reports it with git's own stderr, never aborts the run).
"""

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from django.db import DatabaseError

from teatree.config import OverlayEntry
from teatree.core.models import Worktree
from teatree.core.worktree.relocation import (
    RelocationCandidate,
    active_cwd,
    half_move_target,
    relocation_refusal,
    relocation_target,
)
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


def _matches_overlay(worktree_overlay: str, overlay_name: str) -> bool:
    """Canonical-alias-tolerant overlay match (``teatree`` ≡ ``t3-teatree``)."""
    return OverlayEntry.canonical_overlay_name(worktree_overlay) == OverlayEntry.canonical_overlay_name(overlay_name)


def active_overlay_name() -> str:
    """The active overlay name, resolved exactly as ``config.worktree_root()`` does.

    ``T3_OVERLAY_NAME`` → cwd discovery → the single installed overlay, so the
    relocate scope and the per-overlay ``target_root`` always agree on the overlay.
    """
    from teatree.config.resolution import _resolved_overlay_name  # noqa: PLC0415 — deferred: keeps command import light

    return _resolved_overlay_name(None)


def run_relocate(overlay_name: str, target_root: Path, io: RelocateIO, *, dry_run: bool) -> RelocateResult:
    """Relocate *overlay_name*'s teatree-managed worktrees under *target_root*.

    *target_root* is the resolved per-overlay WORKTREE root
    (``config.worktree_root()``). Each movable worktree is moved with
    ``git worktree move`` and its row's ``extra['worktree_path']`` rewritten;
    every worktree :func:`~teatree.core.worktree.relocation.relocation_refusal`
    rejects is skipped with that reason, the run is idempotent, ``dry_run`` plans
    without touching anything, and one failed move never aborts the rest. A row
    whose recorded path is gone but whose worktree already sits under
    *target_root* (a prior run's git move succeeded then the DB save threw) is
    self-healed — see :func:`_reconcile_half_move`.
    """
    result = RelocateResult(dry_run=dry_run)
    target_root_resolved = target_root.resolve()
    active_path = active_cwd()

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
            target = half_move_target(old, target_root_resolved)
            if target is None:
                _record_skip(result, io, f"{old}: worktree path missing on disk (stale row)")
            else:
                _reconcile_half_move(result, io, worktree, target, dry_run=dry_run)
            continue
        candidate = RelocationCandidate.of(worktree, old)
        reason = relocation_refusal(candidate, target_root_resolved, active_path=active_path)
        if reason is not None:
            _record_skip(result, io, f"{old}: {reason}")
            continue

        target = relocation_target(candidate.old_resolved, target_root_resolved)
        line = f"{old} -> {target}"
        if dry_run:
            result.moved.append(line)
            io.write_out(f"  would move {line}")
            continue
        _move_one(result, io, candidate, target, line)

    return result


def _move_one(result: RelocateResult, io: RelocateIO, candidate: RelocationCandidate, target: Path, line: str) -> None:
    """Execute one ``git worktree move`` + DB-row rewrite, reporting success/failure.

    A failed move carries ``CommandFailedError``'s own text — git's stderr is what
    makes an unforeseen refusal diagnosable at all, so it is never swallowed.
    """
    with suppress(OSError):
        target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # candidate.clone is non-None here: the refusal was "source clone not found" otherwise.
        git.worktree_move(str(candidate.clone), str(candidate.old), str(target))
    except CommandFailedError as exc:
        result.failed.append(f"{line}: {exc}")
        io.write_err(f"  FAILED {candidate.old}: {exc}")
        return
    worktree = candidate.worktree
    try:
        worktree.merge_extra(set_keys={"worktree_path": str(target)})
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
    worktree.merge_extra(set_keys={"worktree_path": str(target)})
    result.moved.append(line)
    io.write_out(f"  reconciled {line}")


def _record_skip(result: RelocateResult, io: RelocateIO, line: str) -> None:
    result.skipped.append(line)
    io.write_out(f"  skipped {line}")
