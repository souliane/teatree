"""Arm the merge critic after a review pass commits."""

import logging
from typing import TYPE_CHECKING

from django.db import transaction

from teatree.core.backend_protocols import PrOpenState
from teatree.core.backend_registry import get_backend_provider
from teatree.core.gates.merge_quality_gate import _arm_merge_quality_critic
from teatree.core.merge.ticket_resolution import resolve_gated_ticket
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.review_verdict import HeadVerdictState, ReviewVerdict
from teatree.core.overlay_loader import get_overlay_for_ticket

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)


def _reviewed_pr_is_current(verdict: ReviewVerdict, ticket: "Ticket") -> bool:
    pr = PullRequest.objects.for_pr(slug=verdict.slug, pr_id=verdict.pr_id).order_by("-pk").first()
    if pr is None or pr.state in {PullRequest.State.MERGED, PullRequest.State.CLOSED}:
        return False
    try:
        host = get_backend_provider().get_code_host_for_url(get_overlay_for_ticket(ticket), pr.url)
        return bool(
            host is not None
            and host.get_pr_open_state(pr_url=pr.url) == PrOpenState.OPEN
            and host.fetch_live_head_sha(slug=verdict.slug, pr_id=verdict.pr_id) == verdict.reviewed_sha
        )
    except Exception:
        logger.warning(
            "Could not verify live PR head before arming critic for %s#%d", verdict.slug, verdict.pr_id, exc_info=True
        )
        return False


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
        if ticket is not None and _reviewed_pr_is_current(verdict, ticket):
            _arm_merge_quality_critic(ticket, verdict.reviewed_sha)

    transaction.on_commit(arm, robust=True)
