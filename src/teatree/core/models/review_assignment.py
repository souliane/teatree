"""Durable review-intent record for the Slack reaction/mention auto-assign loop (#1047).

A Slack reaction or ``@``-mention on a message that references an MR/PR is the
trigger that brings the user onto that MR's reviewer list. This model is the
durable idempotent ledger row for that intent.

Mirrors the :class:`teatree.core.models.pending_chat_injection.PendingChatInjection`
shape — durable, single-row-per-trigger, scoped, idempotent — applied to
reviewer assignment instead of inbound chat injection. The
``(overlay, mr_url, user_id)`` key is the canonical idempotency tuple: the
scanner can over-poll Slack safely because the unique constraint
deduplicates, so the next tick does not re-dispatch the reviewer agent for
an MR it has already seen. The scanner never posts a ``:eyes:`` claim
reaction — a claim reaction belongs to review-DONE, not to discovery
(#113/#86); the row records the *intent* to review, and the FSM transition
path posts the outcome reaction once a review lands.
"""

from dataclasses import dataclass
from typing import ClassVar

from django.db import models
from django.utils import timezone


@dataclass(frozen=True, slots=True)
class ReviewIntent:
    """Origin metadata for a :class:`ReviewAssignment` row.

    The ``(overlay, mr_url, user_id)`` triple is the idempotency key. The
    Slack ``channel``/``slack_ts`` plus ``trigger`` record what produced
    the intent so the audit trail can answer "was this a reaction or a
    mention?" and "which message in which channel?".
    """

    mr_url: str
    user_id: str
    channel: str
    slack_ts: str
    trigger: str
    overlay: str = ""


class ReviewAssignment(models.Model):
    """One user-intent-to-review record for a specific MR seen in Slack.

    The lifecycle is monotonic ``pending`` → ``approved`` (when the MR
    lands an approve transition). ``approved`` is reachable from any
    non-approved state. The historic ``eyes_added`` state is retained as a
    valid enum value for rows written before #113 moved the claim reaction
    off discovery, but no code path transitions a row into it any more —
    the scanner never posts a ``:eyes:`` claim at discovery time.
    """

    class State(models.TextChoices):
        PENDING = "pending"
        EYES_ADDED = "eyes_added"
        APPROVED = "approved"

    class Trigger(models.TextChoices):
        REACTION = "reaction"
        MENTION = "mention"

    overlay = models.CharField(max_length=64, blank=True, default="")
    mr_url = models.URLField(max_length=500)
    user_id = models.CharField(max_length=64)
    channel = models.CharField(max_length=64)
    slack_ts = models.CharField(max_length=64)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    trigger = models.CharField(max_length=16, default="")
    observed_at = models.DateTimeField(default=timezone.now)
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_review_assignment"
        ordering: ClassVar = ["observed_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["overlay", "mr_url", "user_id"],
                name="uniq_reviewassignment_overlay_mr_user",
            ),
        ]

    def __str__(self) -> str:
        return f"review-assignment<{self.pk}:{self.state} {self.mr_url} user={self.user_id}>"

    @classmethod
    def record(cls, intent: ReviewIntent) -> "ReviewAssignment | None":
        """Insert one row idempotently on ``(overlay, mr_url, user_id)``.

        Returns the new row on first observation, ``None`` if a row for
        this ``(overlay, mr_url, user_id)`` already exists. The scanner
        treats ``None`` as "we already saw this — skip dispatch" so the
        same MR + same user never produces two `t3:reviewer` invocations.
        """
        if not intent.mr_url or not intent.user_id:
            return None
        row, created = cls.objects.get_or_create(
            overlay=intent.overlay,
            mr_url=intent.mr_url,
            user_id=intent.user_id,
            defaults={
                "channel": intent.channel,
                "slack_ts": intent.slack_ts,
                "trigger": intent.trigger,
            },
        )
        return row if created else None

    def mark_approved(self) -> bool:
        """Mark this row as ``approved`` when the MR landed an approve transition."""
        updated = (
            type(self)
            .objects.filter(pk=self.pk)
            .exclude(state=self.State.APPROVED)
            .update(state=self.State.APPROVED, approved_at=timezone.now())
        )
        if updated:
            self.refresh_from_db(fields=["state", "approved_at"])
        return bool(updated)

    @classmethod
    def approve_for_mr(cls, *, mr_url: str, overlay: str = "") -> int:
        """Mark every row for *mr_url* as approved.

        Called from the ``PullRequest.approve`` post-transition signal to
        close the reaction-driven loop (#1047): reaction → review_intent
        dispatch → review → approve. The transition is idempotent — a
        re-fire on an already-approved row is a no-op. Returns the count
        of rows that transitioned for audit purposes; ``0`` when none
        were pending or the MR has no row.
        """
        if not mr_url:
            return 0
        transitioned = 0
        rows = cls.objects.filter(mr_url=mr_url, overlay=overlay).exclude(state=cls.State.APPROVED)
        for row in rows:
            if row.mark_approved():
                transitioned += 1
        return transitioned
