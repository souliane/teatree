"""The one branch -> ticket answer for a PR a worktree produced."""

from teatree.core.forge_url import is_synthetic_ticket_url
from teatree.core.models import Ticket, Worktree
from teatree.utils.git_remote import slug_from_remote
from teatree.utils.git_remote_ops import remote_url


def ticket_owning_pr_branch(branch: str, *, slug: str) -> Ticket | None:
    """The one live AUTHOR ticket whose worktree on *branch* sits in repo *slug*, else ``None``.

    Ambiguity answers ``None``: a wrong owner binds the merge gates to another ticket's rubric.
    """
    if not branch or not slug:
        return None
    owners = {
        row.ticket_id: row.ticket
        for row in Worktree.objects.filter(branch=branch).select_related("ticket")
        if _owns(row, branch=branch, slug=slug)
    }
    return next(iter(owners.values())) if len(owners) == 1 else None


def _owns(row: Worktree, *, branch: str, slug: str) -> bool:
    issue_url = row.ticket.issue_url
    return (
        _row_slug(row).casefold() == slug.casefold()
        and row.ticket.role == Ticket.Role.AUTHOR
        and row.ticket.state not in Ticket.marker_release_states()
        and not is_synthetic_ticket_url(issue_url)
        and (not issue_url.startswith("auto:") or issue_url == f"auto:{branch}")
    )


def _row_slug(row: Worktree) -> str:
    """``repo_path`` is a slug or the clone directory's arbitrary leaf, so the leaf is read from origin."""
    if "/" in row.repo_path:
        return row.repo_path
    return slug_from_remote(remote_url(row.worktree_path)) if row.worktree_path else ""
