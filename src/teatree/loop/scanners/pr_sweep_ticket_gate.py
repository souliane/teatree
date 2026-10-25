"""The sweep's pre-merge floors: a held-for-human PR, or an own PR no ticket owns, is never auto-merged."""

from collections.abc import Callable

from teatree.core.merge.ticket_resolution import resolve_gated_ticket
from teatree.loop.scanners.pr_sweep_decision import (
    has_independent_cold_review,
    pr_authored_by_self,
    untrusted_merge_provenance,
)
from teatree.loop.scanners.pr_sweep_types import NO_OWNING_TICKET_REASON, MergeAttempt, PrSummary


def unowned_pr_refusal(pr: PrSummary, *, identities: tuple[str, ...], flag: Callable[..., None]) -> MergeAttempt | None:
    """Refuse an own PR whose gated ticket cannot be resolved: with none every ticket-scoped gate no-ops.

    Waits for a cold review, so a PR the reconciler is about to adopt costs the owner no DM; foreign and bot PRs
    have no ticket by design.
    """
    if not pr_authored_by_self(author=pr.author, self_identities=identities):
        return None
    if resolve_gated_ticket(slug=pr.slug, pr_id=pr.number) is not None:
        return None
    if not has_independent_cold_review(slug=pr.slug, pr_id=pr.number, head_sha=pr.head_sha):
        return None
    flag(slug=pr.slug, pr_id=pr.number, reason=NO_OWNING_TICKET_REASON, url=pr.url)
    return MergeAttempt(slug=pr.slug, pr_id=pr.number, decision="blocked", reason=NO_OWNING_TICKET_REASON, url=pr.url)


def precondition_skip_reason(pr: PrSummary) -> str | None:
    if pr.is_draft:
        return "draft"
    if pr.has_changes_requested:
        return "changes_requested"
    # #3244: a FORK / cross-repo PR always holds for a human, even from a trusted author; unreported provenance
    # fails closed to the identity+visibility author check, ahead of the CLEAR lookup and the solo-overlay merge.
    if untrusted_merge_provenance(pr):
        return "fork_requires_human_approval" if pr.same_repo is False else "untrusted_author_public_repo"
    return None
