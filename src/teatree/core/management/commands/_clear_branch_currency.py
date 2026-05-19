"""Branch-currency pre-flight for the ``ticket clear`` command (#940).

Extracted from :mod:`teatree.core.management.commands.ticket` so the
command file stays under the module-health LOC ceiling. The check runs
BEFORE :meth:`MergeClear.issue` so the CLEAR — if issued — already
points at a current SHA. The cold reviewer then attests the post-merge
tree and the release pipeline does not certify a stale base.
"""

from teatree.core.branch_currency import sha_behind_target
from teatree.core.models import Ticket


def check_clear_branch_currency(reviewed_sha: str, ticket: Ticket | None) -> str | None:
    """Refuse a CLEAR whose ``reviewed_sha`` trails the target branch (#940).

    Resolves the repo from the ticket's worktree (the ``ship_invoking``
    row when present, falling back to the first attached worktree).
    Without a ticket or worktree the check is skipped: the
    branch-currency posture is "if we can verify, refuse; if we can't,
    don't block" — same posture as :mod:`teatree.core.clone_guard`.
    Returns an actionable error string on refusal, or ``None`` to proceed.
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
    staleness = sha_behind_target(repo, reviewed_sha, target)
    if staleness is None:
        return None
    return (
        f"reviewed_sha {reviewed_sha[:8]} is {staleness.behind_count} commit(s) behind "
        f"{target} — merge target into the branch and re-attest the post-merge SHA "
        f"before issuing a CLEAR (the release pipeline must not certify a stale base)."
    )
