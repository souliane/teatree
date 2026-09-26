"""Resolve the ticket a PR/MR belongs to — the one answer every merge-time gate reads.

A merge-time gate whose subject is recorded on the TICKET (merge quality, the
anti-vacuity attestation, the rubric done-gate) holds only a ``(slug, pr_id)`` pair
at the chokepoint, so each needs the same PR→ticket lookup. Keeping that lookup in
one leaf — dependent on ``core.models`` alone — is what lets every caller import it
at module level: the resolver used to live beside the merge-quality gate, whose own
``core.review`` imports make it unreachable from inside ``core.merge`` without a
deferred import, and a second copy of the lookup is how the slug divergence this
package just unified got built in the first place.
"""

from typing import TYPE_CHECKING

from teatree.core.models import MergeClear, PullRequest
from teatree.core.models.review_target import review_target_for_task

if TYPE_CHECKING:
    from teatree.core.models import Task, Ticket


def resolve_gated_ticket(*, slug: str, pr_id: int) -> "Ticket | None":
    """The ticket whose merge is gated, or ``None`` when nothing is gated.

    Resolved from the PR ledger ``(repo, iid)`` FIRST (case-insensitive — GitHub
    slugs are case-insensitive, so a ``Owner/Repo`` row must still match an
    ``owner/repo`` merge). When the PR ledger has no matching row (never recorded,
    or mis-cased before this fix), fall back to the ``MergeClear`` for the same
    ``(slug, pr_id)`` — the CLEAR carries the ticket FK at merge time and is the
    reliable handle. This closes the silent-bypass: a directive PR whose PR-ledger
    row is missing or mis-cased no longer skips the PR-4 gate, because its CLEAR's
    ticket still resolves and the (fail-closed) gate runs.
    """
    pr = PullRequest.objects.filter(repo__iexact=slug, iid=str(pr_id)).select_related("ticket").order_by("-id").first()
    if pr is not None and pr.ticket is not None:
        return pr.ticket
    clear = MergeClear.objects.filter(slug__iexact=slug, pr_id=pr_id).select_related("ticket").order_by("-id").first()
    if clear is not None and clear.ticket is not None:
        return clear.ticket
    return None


def gated_ticket_for_review_task(task: "Task") -> "Ticket | None":
    """The ticket whose merge *task*'s review gates, or ``None`` when it gates none.

    A reviewing task's own ``task.ticket`` is a REVIEWER-ROLE row keyed by the PR url,
    not the ticket the PR delivers — so the rubric a reviewer must grade is never
    reachable from it. Resolving through the PR identity instead means the recorder
    writes under exactly the key :func:`resolve_gated_ticket` reads at merge time, and
    a grade can never land on a ticket the gate does not consult.
    """
    target = review_target_for_task(task)
    if target is None:
        return None
    return resolve_gated_ticket(slug=target.slug, pr_id=target.pr_id)
