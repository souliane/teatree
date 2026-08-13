"""The review-arm the PR sweep fires when it refuses to self-merge (#68).

Split out of :mod:`teatree.loop.scanners.pr_sweep` for the same reason as its
``pr_sweep_*`` siblings: the scanner module holds the decision ladder, and the
mechanics each rung reaches for live beside the rule they serve.

Arming is best-effort by design — every refusal degrades to "no task armed"
rather than aborting the sweep, because the flag-level signal has already
surfaced the PR. Three refusals are deliberate, not defensive. A missing
dispatcher or the flag being off means the overlay never opted in. A PR the
operator did not author is skipped (#2210): ``list_open_prs`` returns every open
PR in a watched repo, colleagues' included, and auto-scheduling a colleague's PR
wastes a dispatch and risks an unattended review note on their work. A PR whose
ticket holds a live EXTERNAL delivery lease is skipped (#2104) — a
hand-dispatched reviewer is already on it, and the loop's own FSM never stamps
that lease, so a genuinely unowned own green PR still arms.

A head carrying an unreconciled HOLD never reaches here at all: the caller
refuses first (#4380). Arming a fresh reviewer over a hold nobody took back is
how a contested head accumulated the later verdict that merged it.
"""

import logging
from dataclasses import dataclass

from teatree.loop.scanners.pr_sweep_decision import own_or_same_repo, pr_ticket_under_external_delivery
from teatree.loop.scanners.pr_sweep_ports import ReviewDispatcher
from teatree.loop.scanners.pr_sweep_types import HeadReview, MergeAttempt, PrSummary

__all__ = ["ReviewArmContext", "arm_cold_review", "held_head_attempt"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReviewArmContext:
    """The sweep collaborators the review-arm needs, bundled per call.

    Mirrors :class:`~teatree.loop.scanners.pr_sweep_branch_update.RemedyContext`.
    *dispatcher* is ``None`` and *enabled* is ``False`` on an overlay that never
    opted into unattended review dispatch; *self_identities* scopes the arm to
    the operator's own PRs; *overlay* attributes the enqueued task.
    """

    dispatcher: ReviewDispatcher | None
    enabled: bool
    self_identities: tuple[str, ...] = ()
    overlay: str = ""


def held_head_attempt(pr: PrSummary, *, review: HeadReview) -> MergeAttempt:
    """The refusal a held head produces — reported, with nothing armed (#4380).

    Lives beside :func:`arm_cold_review` because it is the same doctrine seen from
    the other side: ``review_dispatched`` stays ``False`` because accumulating one
    more verdict on a head somebody is holding is how the newer row came to
    authorise the merge. The verdict refs ride out to the signal so the owner DM
    names who stands where.
    """
    return MergeAttempt(
        slug=pr.slug,
        pr_id=pr.number,
        decision="flag_held",
        reason=review.hold_reason,
        url=pr.url,
        held_verdicts=review.held_verdicts,
        authorizing_verdict=review.authorizing_verdict,
    )


def arm_cold_review(pr: PrSummary, *, ctx: ReviewArmContext) -> bool:
    """Enqueue the claimable review task for *pr*; return whether one was armed."""
    if not ctx.enabled or ctx.dispatcher is None:
        return False
    if not own_or_same_repo(pr, self_identities=ctx.self_identities):
        return False
    if pr_ticket_under_external_delivery(slug=pr.slug, pr_id=pr.number, pr_url=pr.url):
        return False
    try:
        return ctx.dispatcher.enqueue(
            slug=pr.slug,
            pr_id=pr.number,
            head_sha=pr.head_sha,
            pr_url=pr.url,
            overlay=ctx.overlay,
        )
    except Exception:
        logger.exception("pr_sweep failed to enqueue auto-review task for %s#%d", pr.slug, pr.number)
        return False
