"""Which ticket a workspace-scoped command acts on.

Its own module so :mod:`teatree.core.management.commands.workspace` stays a CLI
layer that only coordinates. Resolution is not plumbing: ``provision`` / ``start``
/ ``ready`` / ``teardown`` act on EVERY worktree in a ticket, so they must be
runnable from inside a repo worktree AND from the ticket workspace root that holds
those repo subdirs — two different anchors for one answer.
"""

from pathlib import Path

from teatree.core.intake.resolve import WorktreeNotFoundError, _get_user_cwd, resolve_worktree, workspace_owner_ticket
from teatree.core.models import Ticket


def resolve_workspace_ticket(path: str) -> Ticket:
    """The ticket owning *path*, whether it is a repo worktree or the workspace root.

    Normal worktree resolution first; when that fails because the caller stands at
    the workspace root, the dir is attributed to its owning ticket through
    :func:`workspace_owner_ticket` — the single fail-loud resolver carrying the
    symlink-tolerant, multi-owner policy the auto-register chain uses. Never a
    second hand-rolled check, so the two entry points cannot disagree about owners.
    """
    try:
        anchor = resolve_worktree(path)
        return Ticket.objects.get(pk=anchor.ticket.pk)
    except WorktreeNotFoundError:
        base = Path(path).resolve() if path else Path(_get_user_cwd()).resolve()
        owner = workspace_owner_ticket(base)
        if owner is None:
            raise
        return Ticket.objects.get(pk=owner.pk)


__all__ = ["resolve_workspace_ticket"]
