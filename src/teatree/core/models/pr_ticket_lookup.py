"""Resolve the ticket a ``(slug, pr_id)`` pair belongs to — the shared gate lookup.

Several merge-adjacent gates need the ticket behind a forge PR reference and none
of them is handed one: the merge chokepoint knows only ``(slug, pr_id)``, and a
``review record`` invocation may omit ``--ticket-id`` entirely. Resolving that
pair in each gate separately is how a gate silently stops firing — a mis-cased
slug or a missing PR-ledger row reads as "nothing to gate".

Two independent handles, tried in order: the ``PullRequest`` ledger ``(repo,
iid)`` (case-insensitive — GitHub slugs are, so an ``Owner/Repo`` row must match
an ``owner/repo`` reference) and then the ``MergeClear`` for the same pair, which
carries the ticket FK at merge time. ``None`` means the PR genuinely belongs to
no ticket teatree tracks; a caller treats that as "not gated", never as a pass.
"""

from typing import TYPE_CHECKING

from teatree.core.models.merge_clear import MergeClear
from teatree.core.models.pull_request import PullRequest

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


def ticket_for_pr(*, slug: str, pr_id: int) -> "Ticket | None":
    """The ticket behind ``slug#pr_id``, or ``None`` when no tracked ticket owns it."""
    pr = PullRequest.objects.filter(repo__iexact=slug, iid=str(pr_id)).select_related("ticket").order_by("-id").first()
    if pr is not None and pr.ticket is not None:
        return pr.ticket
    clear = MergeClear.objects.filter(slug__iexact=slug, pr_id=pr_id).select_related("ticket").order_by("-id").first()
    if clear is not None and clear.ticket is not None:
        return clear.ticket
    return None
