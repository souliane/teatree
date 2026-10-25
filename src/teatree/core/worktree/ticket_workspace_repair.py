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
from teatree.config.checkout_roots import checkout_roots
from teatree.config.settings import OverlayEntry
from teatree.core.cleanup.removal_proof import removal_refusal
from teatree.core.cleanup.unshipped_work import (
    ARTIFACT_NAMESPACE,
    COMMITS_SUFFIX,
    FILES_SUFFIX,
    UNCOMMITTED_SUFFIX,
    UnshippedWork,
    bundle_path,
    probe_unshipped_work,
    write_salvage_bundle,
)
from teatree.core.models import Ticket, UnshippedWorkRecord, Worktree
from teatree.core.worktree.clone_paths import git_common_clone_dir
from teatree.core.worktree.ticket_workspace import ticket_workspace_dirs
from teatree.core.worktree.worktree_paths import paths_match, ticket_dir_for
from teatree.paths import get_data_dir
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


def _readd_checkout(clone: Path, old_path: Path, new_path: Path, branch: str, patch: str) -> tuple[str | None, bool]:
    if git.worktree_add(str(clone), str(new_path), branch, create_branch=False):
        return None, False
    # The branch is now free but unchecked-out anywhere; recreate at the OLD
    # path so the checkout — and the row, still pointing at it — survive.
    recovered = git.worktree_add(str(clone), str(old_path), branch, create_branch=False)
    if recovered and (error := _apply_uncommitted_delta(patch, old_path)):
        return (
            (
                f"git worktree add failed for {new_path} "
                f"(recreated at the original path, but delta reapply failed: {error})"
            ),
            True,
        )
    reason = "recreated at the original path" if recovered else "COULD NOT be recreated — inspect the clone"
    return f"git worktree add failed for {new_path} ({reason})", not recovered


def _restore_hint(prefix: str, overlay: str, target: Path | None) -> str:
    if not prefix:
        return ""
    if target is None:
        return f"; salvage bundle: {prefix}; recreate a checkout before restoring"
    name = OverlayEntry.canonical_overlay_name(overlay)
    return f"; t3 {name} workspace restore {prefix} --into {target}"


def _verified_bundle(checkout: Path, branch: str, work: UnshippedWork) -> tuple[str, str]:
    try:
        root = Path(tempfile.mkdtemp(prefix="repair-", dir=get_data_dir(ARTIFACT_NAMESPACE)))
    except OSError as exc:
        return "", f"could not create a salvage directory ({exc}) — nothing moved"
    written = write_salvage_bundle(checkout, branch=branch, root=root)
    if written is None or written.parent != root:
        return "", "could not capture a fresh salvage bundle — nothing moved"
    prefix = str(written)
    expected = (
        (UNCOMMITTED_SUFFIX, work.uncommitted_patch),
        (COMMITS_SUFFIX, work.commits_patch),
        (FILES_SUFFIX, "".join(f"{path}\n" for path in work.dirty_paths)),
    )
    try:
        if any(bundle_path(prefix, suffix).read_bytes() != content.encode("utf-8") for suffix, content in expected):
            return "", "salvage bundle differs from the checkout — nothing moved"
        if not bundle_path(prefix, ".meta").is_file():
            return "", "salvage bundle is incomplete — nothing moved"
    except OSError:
        return "", "salvage bundle is unreadable or incomplete — nothing moved"
    return prefix, ""


def _retire_verified_bundle(prefix: str) -> str:
    if not prefix:
        return ""
    root = Path(prefix).parent
    try:
        for suffix in (UNCOMMITTED_SUFFIX, COMMITS_SUFFIX, FILES_SUFFIX, ".meta"):
            bundle_path(prefix, suffix).unlink(missing_ok=True)
        root.rmdir()
    except OSError as exc:
        return f"retired the salvage record but could not remove {root} ({exc})"
    return ""


def _source_clone(worktree: Worktree, old_path: Path) -> Path | str:
    clone = git_common_clone_dir(str(old_path))
    if clone is None:
        return f"{worktree.repo_path}: could not resolve the clone behind {old_path}"
    actual = git.current_branch(str(old_path))
    if actual != worktree.branch:
        actual_name = "detached HEAD" if actual == "HEAD" else actual
        return (
            f"{worktree.repo_path}: {old_path} is on {actual_name}, "
            f"recorded branch is {worktree.branch} — nothing moved"
        )
    return clone


def _relocate_verified(
    worktree: Worktree,
    clone: Path,
    paths: tuple[Path, Path],
    work: UnshippedWork,
    bundle: tuple[str, str],
) -> str | None:
    old_path, new_path = paths
    prefix, previous = bundle
    prior = f"; previous salvage: {previous}" if prefix and previous else ""
    try:
        new_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"{worktree.repo_path}: could not create {new_path.parent} ({exc}) — nothing moved"
    if not git.worktree_remove(str(clone), str(old_path)):
        named = f"; salvage bundle: {prefix}" if prefix else ""
        return f"{worktree.repo_path}: git worktree remove failed for {old_path} — inspect it{named}{prior}"
    relocate_error, needs_restore = _readd_checkout(clone, old_path, new_path, worktree.branch, work.uncommitted_patch)
    if relocate_error:
        target = old_path if old_path.is_dir() else new_path if new_path.is_dir() else None
        hint = _restore_hint(prefix, worktree.overlay, target) if needs_restore else ""
        return f"{worktree.repo_path}: {relocate_error}{hint}{prior}"

    worktree.merge_extra(set_keys={"worktree_path": str(new_path)})
    if apply_error := _apply_uncommitted_delta(work.uncommitted_patch, new_path):
        hint = _restore_hint(prefix, worktree.overlay, new_path)
        return (
            f"{worktree.repo_path}: moved to {new_path}, but its uncommitted delta failed to reapply: "
            f"{apply_error}{hint}{prior}"
        )

    if cleanup_error := _retire_verified_bundle(prefix):
        return f"{worktree.repo_path}: moved to {new_path}, but {cleanup_error}"
    return None


def _move_one(worktree: Worktree, canonical: Path) -> str | None:
    """Move *worktree*'s checkout into *canonical*; ``None`` on success, else the error."""
    old_path = Path((worktree.extra or {}).get("worktree_path") or "")
    clone = _source_clone(worktree, old_path)
    if isinstance(clone, str):
        return clone

    work = probe_unshipped_work(old_path)
    refusal = work.unreadable or removal_refusal(old_path, clone=clone, row=worktree, allow_changes=True)
    if refusal:
        return f"{worktree.repo_path}: {old_path} is unproven ({refusal}) — refusing to touch it"

    new_path = canonical / Path(worktree.repo_path).name
    if new_path.exists():
        return f"{worktree.repo_path}: {new_path} already exists — refusing to overwrite it"

    prefix = ""
    previous = ""
    if work.exists:
        previous = (
            UnshippedWorkRecord.objects.filter(checkout_path=str(old_path))
            .values_list("artifact_prefix", flat=True)
            .first()
            or ""
        )
        prefix, error = _verified_bundle(old_path, worktree.branch, work)
        if error:
            prior = f"; previous salvage: {previous}" if previous else ""
            return f"{worktree.repo_path}: {error}{prior}"
    else:
        fresh = probe_unshipped_work(old_path)
        if fresh.exists:
            return f"{worktree.repo_path}: checkout changed after the clean read — nothing moved"

    return _relocate_verified(worktree, clone, (old_path, new_path), work, (prefix, previous))


def repair_split_workspace(ticket: Ticket) -> RepairOutcome:
    """Move every divergent worktree of *ticket* into its canonical workspace dir.

    A no-op (nothing moved, nothing kept, no error) when the ticket has at most
    one distinct workspace dir already — there is no split to repair.
    """
    dirs = ticket_workspace_dirs(ticket)
    if len(dirs) <= 1:
        return RepairOutcome()

    canonical = _canonical_dir(ticket)
    if refusal := checkout_roots().refusal(canonical, durable=True):
        return RepairOutcome(errors=[refusal])
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
