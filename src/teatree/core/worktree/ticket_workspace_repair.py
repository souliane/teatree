"""Repair a ticket whose materialised worktrees are split across more than one dir.

:func:`~teatree.core.worktree.ticket_workspace.assert_joins_ticket_workspace` refuses
a NEW checkout that would deepen an existing split; it does not undo one already on
disk. The split-refusal message used to point the operator at ``workspace
clean-all``, but that command is the DONE-worktree reaper — it deliberately KEEPS an
unfinished checkout (#706/#835), so following the advice on a still-open ticket
leaves the split exactly as it was. This module is the targeted recovery: it moves
each divergent checkout into the ticket's canonical workspace dir.

The move is a ``git worktree remove`` + ``git worktree add <path> <branch>`` pair,
never a rename — a rename(2) is refused across the bind-mount boundary that
produces the split this invariant exists to prevent (#111), while remove+add works
from any mount. Commits stay reachable from the branch inside the CLONE regardless
of which worktree checked them out, so only the WORKING TREE delta (staged,
unstaged, untracked) needs explicit preservation — captured before the remove and
reapplied after the add.
"""

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from teatree.config import worktree_root
from teatree.core.cleanup.unshipped_work import probe_unshipped_work
from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.clone_paths import git_common_clone_dir
from teatree.core.worktree.ticket_workspace import ticket_workspace_dirs
from teatree.core.worktree.worktree_paths import paths_match, ticket_dir_for
from teatree.utils import git
from teatree.utils.git_run import git_env_without_overrides, run_with_status


@dataclass(frozen=True, slots=True)
class RepairOutcome:
    """Per-repo disposition of one repair pass — never partial-silent."""

    moved: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        lines = [f"moved: {', '.join(self.moved) or '(none)'}"]
        if self.kept:
            lines.append(f"already canonical: {', '.join(self.kept)}")
        lines += [f"ERROR: {e}" for e in self.errors]
        return "\n".join(lines)


def _canonical_dir(ticket: Ticket) -> Path:
    branch = (ticket.extra or {}).get("branch", "")
    return ticket_dir_for(worktree_root(overlay=ticket.overlay or None), branch)


def _apply_uncommitted_delta(patch: str, into: Path) -> str | None:
    """Reapply the working-tree delta at *into*; ``None`` on success, else the error."""
    if not patch:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as handle:
        handle.write(patch)
        patch_path = handle.name
    try:
        result = run_with_status(repo=str(into), args=["apply", patch_path], env=git_env_without_overrides())
    finally:
        Path(patch_path).unlink(missing_ok=True)
    if result.returncode == 0:
        return None
    return result.stderr.strip() or f"git apply exited {result.returncode}"


def _relocate_checkout(clone: Path, old_path: Path, new_path: Path, branch: str) -> str | None:
    """``git worktree remove`` + ``add`` from *old_path* to *new_path*; ``None`` on success."""
    if not git.worktree_remove(str(clone), str(old_path)):
        return f"git worktree remove failed for {old_path} — nothing was moved"
    if git.worktree_add(str(clone), str(new_path), branch, create_branch=False):
        return None
    # The branch is now free but unchecked-out anywhere; recreate at the OLD
    # path so the checkout — and the row, still pointing at it — survive.
    recovered = git.worktree_add(str(clone), str(old_path), branch, create_branch=False)
    reason = "recreated at the original path" if recovered else "COULD NOT be recreated — inspect the clone"
    return f"git worktree add failed for {new_path} ({reason})"


def _move_one(worktree: Worktree, canonical: Path) -> str | None:
    """Move *worktree*'s checkout into *canonical*; ``None`` on success, else the error."""
    old_path = Path((worktree.extra or {}).get("worktree_path") or "")
    clone = git_common_clone_dir(str(old_path))
    if clone is None:
        return f"{worktree.repo_path}: could not resolve the clone behind {old_path}"

    work = probe_unshipped_work(old_path)
    if work.unreadable:
        return f"{worktree.repo_path}: {old_path} is unreadable ({work.unreadable}) — refusing to touch it"

    new_path = canonical / Path(worktree.repo_path).name
    if new_path.exists():
        return f"{worktree.repo_path}: {new_path} already exists — refusing to overwrite it"

    canonical.mkdir(parents=True, exist_ok=True)
    if relocate_error := _relocate_checkout(clone, old_path, new_path, worktree.branch):
        return f"{worktree.repo_path}: {relocate_error}"

    if apply_error := _apply_uncommitted_delta(work.uncommitted_patch, new_path):
        return f"{worktree.repo_path}: moved to {new_path}, but its uncommitted delta failed to reapply: {apply_error}"

    worktree.merge_extra(set_keys={"worktree_path": str(new_path)})
    return None


def repair_split_workspace(ticket: Ticket) -> RepairOutcome:
    """Move every divergent worktree of *ticket* into its canonical workspace dir.

    A no-op (nothing moved, nothing kept, no error) when the ticket has at most
    one distinct workspace dir already — there is no split to repair.
    """
    dirs = ticket_workspace_dirs(ticket)
    if len(dirs) <= 1:
        return RepairOutcome()

    canonical = _canonical_dir(ticket)
    moved: list[str] = []
    kept: list[str] = []
    errors: list[str] = []
    for worktree in Worktree.objects.for_ticket(ticket):
        path = (worktree.extra or {}).get("worktree_path") or ""
        if not path or not Path(path).is_dir():
            continue
        if paths_match(Path(path).parent, canonical):
            kept.append(worktree.repo_path)
            continue
        if error := _move_one(worktree, canonical):
            errors.append(error)
        else:
            moved.append(worktree.repo_path)
    return RepairOutcome(moved=moved, kept=kept, errors=errors)


__all__ = ["RepairOutcome", "repair_split_workspace"]
