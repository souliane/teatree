"""Arm the merge critic after a review pass commits."""

from django.db import transaction

from teatree.core.gates.merge_quality_gate import _arm_merge_quality_critic
from teatree.core.merge.ticket_resolution import resolve_gated_ticket
from teatree.core.models.review_verdict import HeadVerdictState, ReviewVerdict


def arm_for_recorded_review(recorded: ReviewVerdict) -> None:
    if not recorded.is_merge_safe():
        return

    def arm() -> None:
        verdict = ReviewVerdict.objects.get(pk=recorded.pk)
        if (
            ReviewVerdict.objects.effective_state_at(
                slug=verdict.slug, pr_id=verdict.pr_id, head_sha=verdict.reviewed_sha
            )
            != HeadVerdictState.MERGE_SAFE
        ):
            return
        ticket = resolve_gated_ticket(slug=verdict.slug, pr_id=verdict.pr_id)
        if ticket is not None:
            _arm_merge_quality_critic(ticket, verdict.reviewed_sha)

    transaction.on_commit(arm, robust=True)
