"""The review-arm the PR sweep fires when it refuses to self-merge (#68).

Split out of :mod:`teatree.loop.scanners.pr_sweep` for the same reason as its
``pr_sweep_*`` siblings: the scanner module holds the decision ladder, and the
mechanics each rung reaches for live beside the rule they serve.

Arming is best-effort by design — every refusal degrades to "no task armed"
rather than aborting the sweep, because the flag-level signal has already
surfaced the PR. Four refusals are deliberate, not defensive. A missing
dispatcher or the flag being off means the overlay never opted in. A PR the
operator did not author is skipped (#2210): ``list_open_prs`` returns every open
PR in a watched repo, colleagues' included, and auto-scheduling a colleague's PR
wastes a dispatch and risks an unattended review note on their work. A PR whose
ticket holds a live EXTERNAL delivery lease is skipped (#2104) — a
hand-dispatched reviewer is already on it, and the loop's own FSM never stamps
that lease, so a genuinely unowned own green PR still arms. A PR a reviewing task
is ALREADY open on is skipped for the same reason with a different owner: the
self-PR scanner's reviewer dedups through its own ledger, which this arm's
per-head claim cannot see, so one green head bought two reviews of one tree.

A head carrying an unreconciled HOLD never reaches here at all: the caller
refuses first (#4380). Arming a fresh reviewer over a hold still active at this head is
how a contested head accumulated the later verdict that merged it.
"""

import logging
from dataclasses import dataclass

from django.db.models import Q

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models.head_carry_over import HeadCarryOver
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.core.models.round_metric import RoundMetric
from teatree.core.notify import notify_user
from teatree.core.notify_types import NotifyKind
from teatree.loop.pr_ticket_index import resolve_author_ticket
from teatree.loop.scanners.pr_sweep_decision import own_or_same_repo, pr_ticket_under_external_delivery
from teatree.loop.scanners.pr_sweep_ports import ReviewDispatcher
from teatree.loop.scanners.pr_sweep_types import CLEAR_PRESENT_UNUSABLE_REASON, HeadReview, MergeAttempt, PrSummary

__all__ = ["ReviewArmContext", "arm_cold_review", "held_head_attempt", "review_in_flight_for_pr"]

logger = logging.getLogger(__name__)
MAX_REVIEW_ROUNDS = 3
REVIEW_ROUNDS_EXHAUSTED = "review_rounds_exhausted"


def review_rounds_exhausted(*, slug: str, pr_id: int, pr_url: str, head_sha: str) -> bool:
    ticket = resolve_author_ticket(slug=slug, pr_id=pr_id, pr_url=pr_url)
    if ticket is None:
        return False
    verdicts = list(
        ReviewVerdict.objects.filter(Q(ticket=ticket) | Q(ticket__isnull=True, slug__iexact=slug, pr_id=pr_id))
        .exclude(verdict=ReviewVerdict.Verdict.UNVERIFIED)
        .order_by("recorded_at", "pk")
        .values_list("slug", "pr_id", "reviewed_sha")
    )
    carried = {
        (row.slug.casefold(), row.pr_id, row.to_head)
        for row in HeadCarryOver.objects.filter(to_head__in=[head for _, _, head in verdicts])
        if row.reviewed_by_carry
    }
    measured = (
        RoundMetric.objects.filter(ticket=ticket, kind=RoundMetric.Kind.REVIEW)
        .exclude(outcome__in=("", "venue_limit"))
        .values_list("subject", flat=True)
    )
    heads = tuple(
        dict.fromkeys(
            (
                *measured,
                *(
                    head
                    for record_slug, record_pr_id, head in verdicts
                    if (record_slug.casefold(), record_pr_id, head) not in carried
                ),
            )
        )
    )
    if head_sha in heads or len(heads) < MAX_REVIEW_ROUNDS:
        return False
    text = (
        f"Review limit reached for {ticket.issue_url or ticket.pk} and {pr_url}: "
        f"{MAX_REVIEW_ROUNDS} recorded heads ({', '.join(heads)}). Review the findings before another dispatch."
    )
    notify_user(
        text,
        kind=NotifyKind.INFO,
        idempotency_key=f"review_rounds_exhausted:{ticket.pk}:{pr_id}",
        audience=NotifyAudience.OWNER_ESCALATION,
    )
    return True


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


def review_in_flight_for_pr(*, pr_url: str) -> bool:
    """True iff a reviewing Task is already open on the ticket that IS this PR.

    Two arms reach the same head. The self-PR scanner mints its reviewer through
    :class:`~teatree.core.models.codex_review_marker.CodexReviewMarker`; this sweep
    arms one through :class:`~teatree.core.models.auto_review_dispatch.AutoReviewDispatch`.
    Each ledger dedups per head, but they dedup INDEPENDENTLY, so a green head
    reliably bought two cold reviews of one tree. The per-MR
    :class:`~teatree.core.models.mr_review_lock.MRReviewLock` the arm already takes
    says exactly this, but the other arm's task never takes it, so the question is
    asked here rather than by admitting a second writer to that lock.

    Deferring costs the sweep nothing: the in-flight reviewer records the very
    verdict the next sweep merges (or holds) on. Only a NON-terminal task counts, so
    a reviewer that died leaves the head armable on the next pass — this defers a
    review, it can never latch one out.
    """
    from teatree.core.models import Task  # noqa: PLC0415 — lazy ORM import

    if not pr_url:
        return False
    return Task.objects.pending_in_phase("reviewing").filter(ticket__issue_url=pr_url).exists()


def held_head_attempt(pr: PrSummary, *, review: HeadReview) -> MergeAttempt:
    """The refusal a held head produces — reported, with nothing armed (#4380).

    Lives beside :func:`arm_cold_review` because it is the same doctrine seen from
    the other side: ``review_dispatched`` stays ``False`` because accumulating one
    more verdict on a head somebody is holding is how the newer row came to
    authorise the merge. The verdict refs ride out to the signal so the owner DM
    names who stands where — nothing merged here, so ``authorizing_verdict`` carries
    the PASS standing beside the hold, whichever of the two was recorded last.
    """
    return MergeAttempt(
        slug=pr.slug,
        pr_id=pr.number,
        decision="flag_held",
        reason=review.hold_reason,
        url=pr.url,
        held_verdicts=review.held_verdicts,
        authorizing_verdict=review.standing_merge_safe,
    )


def arm_cold_review(pr: PrSummary, *, ctx: ReviewArmContext) -> bool:
    """Enqueue the claimable review task for *pr*; return whether one was armed."""
    if not ctx.enabled or ctx.dispatcher is None:
        return False
    if not own_or_same_repo(pr, self_identities=ctx.self_identities):
        return False
    if pr_ticket_under_external_delivery(slug=pr.slug, pr_id=pr.number, pr_url=pr.url) or review_rounds_exhausted(
        slug=pr.slug, pr_id=pr.number, pr_url=pr.url, head_sha=pr.head_sha
    ):
        return False
    if review_in_flight_for_pr(pr_url=pr.url):
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


def no_review_attempt(pr: PrSummary, *, unusable_clear: bool, exhausted: bool, ctx: ReviewArmContext) -> MergeAttempt:
    return MergeAttempt(
        slug=pr.slug,
        pr_id=pr.number,
        decision="flag_no_review",
        reason=REVIEW_ROUNDS_EXHAUSTED
        if exhausted
        else (CLEAR_PRESENT_UNUSABLE_REASON if unusable_clear else "solo_overlay_no_review"),
        url=pr.url,
        review_dispatched=False if exhausted else arm_cold_review(pr, ctx=ctx),
    )
