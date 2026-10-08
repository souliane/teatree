"""The sweep's ownership floor: an own PR no ticket owns is never auto-merged."""

from collections.abc import Callable

from teatree.core.merge.ticket_resolution import resolve_gated_ticket
from teatree.loop.scanners.pr_sweep_decision import has_independent_cold_review, pr_authored_by_self
from teatree.loop.scanners.pr_sweep_types import NO_OWNING_TICKET_REASON, MergeAttempt, PrSummary


def unowned_pr_refusal(pr: PrSummary, *, identities: tuple[str, ...], flag: Callable[..., None]) -> MergeAttempt | None:
    """Refuse an own PR whose gated ticket cannot be resolved: with none every ticket-scoped gate no-ops.

    Waits for a cold review, so a PR the reconciler is about to adopt costs the owner no DM; foreign and bot PRs
    have no ticket by design.
    """
    if not pr_authored_by_self(author=pr.author, self_identities=identities):
        return None
    if not has_independent_cold_review(slug=pr.slug, pr_id=pr.number, head_sha=pr.head_sha):
        return None
    if resolve_gated_ticket(slug=pr.slug, pr_id=pr.number) is not None:
        return None
    flag(slug=pr.slug, pr_id=pr.number, reason=NO_OWNING_TICKET_REASON, url=pr.url)
    return MergeAttempt(slug=pr.slug, pr_id=pr.number, decision="blocked", reason=NO_OWNING_TICKET_REASON, url=pr.url)
