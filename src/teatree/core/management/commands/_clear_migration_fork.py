"""Pre-merge migration-fork pre-flight for the ``ticket clear`` command (#995).

The command-layer sibling of :mod:`_clear_branch_currency`: it resolves
the invoking worktree's repo the same way and runs
:func:`teatree.core.migration_leaf_probe.sha_forks_migration_graph`
BEFORE :meth:`MergeClear.issue`. A forked migration graph (two branches
each adding a migration off the same parent) reaches ``clear+merge`` with
CI green, then breaks the post-merge self-DB ``migrate`` with
``Conflicting migrations detected``; this catches it at CLEAR time so the
gate never certifies a state that is not end-to-end mergeable.

Without a resolvable ticket/worktree the check is skipped — the same "if
we can verify, refuse; if we can't, don't block" posture as
:mod:`_clear_branch_currency`.
"""

from teatree.core.migration_leaf_probe import sha_forks_migration_graph
from teatree.core.models import Ticket

MIGRATION_LEAF_CONFLICT_REASON = "migration_leaf_conflict"


def check_clear_migration_fork(reviewed_sha: str, ticket: Ticket | None) -> str | None:
    """Refuse a CLEAR when ``reviewed_sha`` merged onto target forks the migration graph.

    Resolves the repo from the ticket's worktree (the ``ship_invoking``
    row when present, else the first attached worktree), exactly as
    :func:`check_clear_branch_currency` does. Returns an actionable error
    string naming the conflicting migration leaves on a real fork, or
    ``None`` to proceed (no ticket/worktree, or a linear graph).
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
    conflict = sha_forks_migration_graph(repo, reviewed_sha, target)
    if conflict is None:
        return None
    leaves = ", ".join(conflict.leaf_names)
    return (
        f"[{MIGRATION_LEAF_CONFLICT_REASON}] merging {reviewed_sha[:8]} onto {target} would fork "
        f"the {conflict.app_label!r} migration graph: {conflict.leaf_count} leaf nodes ({leaves}). "
        f"Two migrations branch off the same parent — the post-merge `migrate` would fail with "
        f"'Conflicting migrations detected'. Merge {target} into the branch, run "
        f"`python manage.py makemigrations --merge` to reconcile the leaves, and re-attest the "
        f"post-merge SHA before issuing a CLEAR."
    )
