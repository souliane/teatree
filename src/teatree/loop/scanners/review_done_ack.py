"""Post the review-DONE Slack ack from the FSM fact, not from a CLI call.

The two-step review-pickup design is real and fully built
(:mod:`teatree.loop.review_claim`): no ``:eyes:`` at discovery (#113/#86),
then ``:eyes:`` + a verdict emoji on the MR's broadcast message once a review
actually lands. What was missing is the *trigger*.

``emit_review_done_reactions`` had exactly ONE caller — ``review record``'s
``_emit_review_done_signal``. So the colleague-visible ack fired only when the
reviewer sub-agent remembered to run ``t3 <overlay> review record``. A review
that posted its inline findings on the MR and completed its task without that
call produced no reaction at all — which is what happened on 2026-07-22: four
colleague MRs got real inline notes (and one an approval) with no
``ReviewVerdict`` row behind them, hence no ack. The Slack transport is not
implicated: with no verdict recorded the reaction path was never entered.

This scanner rebinds the ack to the durable fact instead of the optional
command: a reviewer-role ticket that reached ``REVIEW_POSTED`` HAS had its review
posted. The verdict emoji still comes from a recorded ``ReviewVerdict`` when
one exists; with none, ``:eyes:`` alone is the honest signal — "this was
picked up and reviewed" — which is the thing colleagues had no way to see.

Bounded, idempotent, and never a backfill burst:

* Only tickets whose reviewing task completed within ``max_age_hours`` are
    considered, so enabling this does not re-react across the whole history.
* ``emit_review_done_reactions`` skips any emoji already present (colleague,
    bot, or the :class:`OutboundClaim` ledger) and records a claim on success,
    so a later tick is a no-op.
* Reacting routes through ``OnBehalfSlackEgress`` like every other
    colleague-surface post, so the on-behalf gate and audit still apply.
* An MR that was never broadcast to Slack has no message to react on;
    ``emit_review_done_reactions`` returns ``[]`` and nothing is posted.
"""

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from django.utils import timezone

from teatree.core.backend_protocols import MessagingBackend
from teatree.loop.review_done_reactions import emit_review_done_reactions
from teatree.loop.scanners.base import ScanSignal
from teatree.utils.url_slug import pr_ref_from_url

if TYPE_CHECKING:
    from teatree.core.models import Task, Ticket

logger = logging.getLogger(__name__)

_FALLBACK_EMOJIS: tuple[str, ...] = ("eyes",)


@dataclass(slots=True)
class ReviewDoneAckScanner:
    """React review-DONE on the Slack broadcast of every freshly-reviewed colleague MR.

    ``overlay_name`` scopes the walk so one overlay does not ack a sibling's
    MRs. ``max_age_hours`` bounds it to recent reviews — the window is what
    keeps first enablement from re-reacting across every historical review.
    """

    messaging: MessagingBackend
    overlay_name: str = ""
    max_age_hours: int = 24
    name: str = field(default="review_done_ack", init=False)

    def scan(self) -> list[ScanSignal]:
        return [signal for ticket in self._recently_reviewed_tickets() if (signal := self._ack(ticket)) is not None]

    def _recently_reviewed_tickets(self) -> list["Ticket"]:
        """Reviewer-role tickets whose review completed inside the recency window."""
        from django.apps import apps  # noqa: PLC0415 — deferred: app registry read at call time

        ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))
        task_model = cast("type[Task]", apps.get_model("core", "Task"))
        cutoff: dt.datetime = timezone.now() - dt.timedelta(hours=self.max_age_hours)
        candidates = ticket_model.objects.filter(
            role="reviewer",
            state=ticket_model.State.REVIEW_POSTED,
            tasks__phase="reviewing",
            tasks__status=task_model.Status.COMPLETED,
            tasks__created_at__gte=cutoff,
        ).exclude(issue_url="")
        if self.overlay_name:
            candidates = candidates.filter(overlay=self.overlay_name)
        return list(candidates.order_by("pk").distinct())

    def _ack(self, ticket: "Ticket") -> ScanSignal | None:
        url = ticket.issue_url
        ref = pr_ref_from_url(url)
        if ref is None:
            return None
        try:
            posted = emit_review_done_reactions(
                slug=ref.slug,
                pr_id=ref.pr_id,
                emojis=_emojis_for(ref.slug, ref.pr_id),
                messaging=self.messaging,
            )
        except Exception:
            logger.exception("ReviewDoneAckScanner failed to ack %s", url)
            return None
        if not posted:
            return None
        return ScanSignal(
            kind="review_done_ack.reacted",
            summary=f"Reacted {' '.join(f':{emoji}:' for emoji in posted)} on the review broadcast for {url}",
            payload={"mr_url": url, "emojis": list(posted), "overlay": self.overlay_name},
        )


def _emojis_for(slug: str, pr_id: int) -> tuple[str, ...]:
    """The recorded verdict's emoji set, else the bare ``:eyes:`` review-DONE signal."""
    from teatree.core.models import ReviewVerdict  # noqa: PLC0415 — deferred: ORM import needs the app registry

    recorded = ReviewVerdict.objects.latest_for_pr(slug, pr_id)
    return recorded.done_reaction_emojis() if recorded is not None else _FALLBACK_EMOJIS
