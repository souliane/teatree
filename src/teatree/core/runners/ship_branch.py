"""Reconcile a shipping worktree's recorded branch with its real git branch."""

import logging
from typing import TYPE_CHECKING, cast

from teatree.utils import git

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket
    from teatree.core.models.types import TicketExtra
    from teatree.core.models.worktree import Worktree

logger = logging.getLogger(__name__)


def resolve_and_reconcile_branch(ticket: "Ticket", worktree: "Worktree", repo_path: str) -> str:
    """Return the worktree's actual branch, reconciling the DB to a valid rename.

    A detached HEAD or unrelated branch falls back to the recorded value. A
    ticket-prefixed rename updates both the worktree and matching ticket extras
    so subsequent pre-push, ship, and merge readers see the same ref (#1519).
    """
    recorded = worktree.branch
    current = git.current_branch(repo=repo_path)
    prefix = f"{ticket.ticket_number}-"
    if not current or current == "HEAD" or not current.startswith(prefix):
        if current and current != recorded:
            logger.warning(
                "Ship branch resolution for ticket %s: worktree at %s is on %r "
                "(detached or not prefixed %r) — falling back to recorded branch %r",
                ticket.ticket_number,
                repo_path,
                current,
                prefix,
                recorded,
            )
        return recorded
    if current != recorded:
        logger.info(
            "Ship reconciling ticket %s worktree branch %r → %r (renamed in the worktree)",
            ticket.ticket_number,
            recorded,
            current,
        )
        worktree.branch = current
        worktree.save(update_fields=["branch"])
        extra = ticket.extra or {}
        renamed = {key: current for key in ("branch", "ship_invoking_branch") if extra.get(key) == recorded}
        if renamed:
            ticket.merge_extra(set_keys=cast("TicketExtra", renamed))
    return current
