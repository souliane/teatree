"""Branch-currency pre-flight for the ``ticket clear`` command (#940).

Extracted from :mod:`teatree.core.management.commands.ticket` so the
command file stays under the module-health LOC ceiling. The check runs
BEFORE :meth:`MergeClear.issue`.

**Conflict-only (#940, relaxed).** A CLEAR is refused only when the
reviewed SHA both trails the target branch AND the merge would produce
real conflicts an automatic squash-merge could not resolve. A branch
that is merely behind but conflict-free is allowed through: requiring a
rebase/update-branch in that case adds friction without safety, since
GitHub re-applies the branch's diff onto the live target at squash-merge
time and the merge-time live-CI re-check still guards correctness.
"""

from teatree.core.models import Ticket
from teatree.core.worktree.branch_currency import sha_conflicts_with_target


def check_clear_branch_currency(reviewed_sha: str, ticket: Ticket | None) -> str | None:
    """Refuse a CLEAR only when ``reviewed_sha`` *conflicts* with target (#940).

    Resolves the repo from the ticket's worktree (the ``ship_invoking``
    row when present, falling back to the first attached worktree).
    Without a ticket or worktree the check is skipped: the
    branch-currency posture is "if we can verify, refuse; if we can't,
    don't block" — same posture as :mod:`teatree.core.gates.clone_guard`.
    Returns an actionable error string on a real conflict, or ``None``
    to proceed (including the behind-but-mergeable case).
    """
    if ticket is None:
        return None
    extra = ticket.extra or {}
    invoking = str(extra.get("ship_invoking_branch") or "")
    worktree = None
    if invoking:
        worktree = ticket.worktrees.filter(branch=invoking).first()  # ty: ignore[unresolved-attribute]
    if worktree is None:
        worktree = ticket.worktrees.first()  # ty: ignore[unresolved-attribute]
    if worktree is None:
        return None
    repo = (worktree.extra or {}).get("worktree_path", "") or worktree.repo_path
    if not repo:
        return None
    explicit = str(extra.get("target_branch") or "").strip()
    target = (explicit if "/" in explicit else f"origin/{explicit}") if explicit else "origin/main"
    conflict = sha_conflicts_with_target(repo, reviewed_sha, target)
    if conflict is None:
        return None
    paths_str = ", ".join(conflict.conflicting_paths) if conflict.conflicting_paths else "(see git status)"
    return (
        f"reviewed_sha {reviewed_sha[:8]} conflicts with {target} in: {paths_str}. "
        f"Merge {target} into the branch, resolve the conflicts, and re-attest the "
        f"post-merge SHA before issuing a CLEAR. (Being behind alone is fine — only a "
        f"real merge conflict blocks the CLEAR.)"
    )
