"""The synthetic ``auto:<branch>`` ticket a repo-only checkout attaches to.

Shared by the implicit registration inside :mod:`teatree.core.intake.resolve` and the
explicit ``worktree adopt`` verb, so a checkout the attribution chain cannot place
lands on ONE ticket whichever door it came through — two spellings of this fork would
fork two tickets for one branch.
"""

from teatree.core.intake.ticket_kind_classification import classify_ticket_kind
from teatree.core.models import Ticket


def synthetic_branch_ticket(branch: str, *, repo_name: str, overlay_name: str) -> Ticket:
    """Get-or-create the ``auto:<branch>`` ticket for an unattributable checkout.

    A synthetic URL names no repo, so ``Ticket.save()``'s ``infer_overlay`` can never
    attribute the row — *overlay_name* (the checkout's own overlay) is the only signal
    there is, and a blank one makes ``get_overlay_for_ticket`` fall through to the
    ambiguous ``get_overlay(None)`` on a multi-overlay install (souliane/teatree#1814).
    """
    return Ticket.objects.get_or_create(
        issue_url=f"auto:{branch}",
        defaults={
            "overlay": overlay_name,
            "variant": "",
            "repos": [repo_name],
            "kind": classify_ticket_kind(title=branch),
        },
    )[0]
