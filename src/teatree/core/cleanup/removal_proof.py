"""Fail-closed proof shared by checkout-removal paths."""

from pathlib import Path

from teatree.core.cleanup.ignored_files import is_orchestration_debris
from teatree.core.cleanup.unshipped_work import UnshippedWork, probe_unshipped_work
from teatree.core.forge_pr_probe import find_open_pr_for_branch
from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.branch_classification import effective_default_target
from teatree.core.worktree.branch_verdict import branch_landed_for_teardown
from teatree.core.worktree.worktree_paths import paths_match
from teatree.utils import git
from teatree.utils.run import CommandFailedError


class RemovalRefusedError(RuntimeError):
    pass


def _tip_landed(checkout: Path, ref: str, target: str) -> bool:
    return branch_landed_for_teardown(str(checkout), ref, target)


def _commit_refusal(checkout: Path, clone: Path, work: UnshippedWork, ref: str) -> str:
    if not work.unpushed_commits:
        return ""
    target = effective_default_target(str(clone))
    if f"refs/remotes/{target}" in work.live_refs and _tip_landed(checkout, ref, target):
        return ""
    return f"{len(work.unpushed_commits)} commit(s) on no live remote ref"


def _row_refusal(checkout: Path, row: Worktree | None, *, sweep: bool, target: str) -> str:
    ticket_id = row.ticket_id if row is not None else None
    for peer in Worktree.objects.sharing_checkout(checkout, ticket_id=ticket_id, path_matches=paths_match):
        if peer.ticket.state not in Ticket.marker_release_states():
            ticket_ref = peer.ticket.ticket_number or peer.ticket_id
            return f"in-flight ticket {ticket_ref} shares the checkout"
    if sweep and row is not None and row.ticket.state not in Ticket.marker_release_states():
        count = git.run_strict(repo=str(checkout), args=["rev-list", "--count", f"{target}..HEAD"])
        if count == "0":
            return "fresh checkout on an in-flight ticket"
    return ""


def _forge_refusal(checkout: Path, branch: str) -> str:
    if branch == git.DETACHED_HEAD:
        return ""
    probe = find_open_pr_for_branch(checkout, branch)
    if probe.is_found:
        return f"open merge request exists: {probe.url}"
    if probe.is_unknown:
        return "the forge cannot say whether a merge request is open"
    return ""


def removal_refusal(
    checkout: Path,
    *,
    clone: Path,
    row: Worktree | None = None,
    sweep: bool = False,
    allow_changes: bool = False,
) -> str:
    if not checkout.is_dir():
        return ""
    prefix = f"{checkout}: not proven shipped — "
    try:
        empty = next(checkout.iterdir(), None) is None
    except OSError as exc:
        return prefix + f"could not read directory ({exc})"
    if empty:
        if sweep and row is not None and row.ticket.state not in Ticket.marker_release_states():
            return prefix + f"in-flight ticket {row.ticket.ticket_number or row.ticket_id} owns the checkout"
        return prefix + reason if (reason := _row_refusal(checkout, row, sweep=False, target="")) else ""
    work = probe_unshipped_work(checkout)
    ref = "HEAD"
    if work.unreadable == f"{checkout}: no resolvable HEAD to diff against" and (
        recovered := git.recovered_head_sha_after_ref_gone(str(checkout))
    ):
        ref = recovered
        work = probe_unshipped_work(checkout, base_ref=ref)
    unsafe_paths = (
        work.ignored_paths
        if allow_changes
        else [path for path in work.dirty_paths if not is_orchestration_debris(path)]
    )
    reason = work.unreadable
    if not reason and unsafe_paths:
        reason = f"{len(unsafe_paths)} changed or ignored file(s): {', '.join(unsafe_paths[:3])}"
    branch = ""
    if not reason:
        try:
            branch = git.current_branch(str(checkout))
            target = effective_default_target(str(clone))
            reason = _commit_refusal(checkout, clone, work, ref)
            reason = reason or _row_refusal(checkout, row, sweep=sweep, target=target)
        except (RuntimeError, CommandFailedError) as exc:
            reason = f"could not verify commits ({exc})"
    if not reason:
        reason = "no remote answers" if work.remote_unverified else _forge_refusal(checkout, branch)
    return prefix + reason if reason else ""
