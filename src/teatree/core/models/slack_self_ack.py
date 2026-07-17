"""Idempotency ledger for the owner-reaction self-ack (👀-back).

When the owner reacts (any emoji) to a message teatree ITSELF authored,
teatree posts a ``:eyes:`` reaction back on that same message so the owner
sees the loop noticed their reaction. This model is the durable idempotency
row for that ack: one row per acked ``(overlay, channel, item_ts)`` so a
scanner that re-observes the same ``reaction_added`` event over successive
ticks never re-posts the ack.

Mirrors :class:`teatree.core.models.review_assignment.ReviewAssignment` in
shape — a single idempotent :meth:`record` classmethod keyed on the tuple —
kept as its own minimal ledger rather than overloading
:class:`teatree.core.models.outbound_claim.OutboundClaim` (whose drift
verifier would try to confirm the reaction and DM the owner on a miss).
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class SlackSelfAckReaction(models.Model):
    """One posted 👀-back ack on a bot-authored message the owner reacted to.

    ``(overlay, channel, item_ts)`` is the idempotency key: the reactor can
    over-observe the owner's ``reaction_added`` event safely because the
    unique constraint deduplicates, so a repeat tick does not re-react.
    """

    overlay = models.CharField(max_length=64, blank=True, default="")
    channel = models.CharField(max_length=64)
    item_ts = models.CharField(max_length=64)
    reacted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_slack_self_ack_reaction"
        ordering: ClassVar = ["reacted_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["overlay", "channel", "item_ts"],
                name="uniq_selfack_overlay_channel_ts",
            ),
        ]

    def __str__(self) -> str:
        return f"slack-self-ack<{self.overlay or '-'} {self.channel}/{self.item_ts}>"

    @classmethod
    def record(cls, *, overlay: str, channel: str, item_ts: str) -> "SlackSelfAckReaction | None":
        """Claim the ack for ``(overlay, channel, item_ts)`` idempotently.

        Returns the new row on the FIRST observation (the caller then posts
        the 👀 reaction), or ``None`` when a row already exists (already
        acked — the caller skips). A failed post releases the claim by
        deleting the returned row so a transient failure retries.
        """
        if not channel or not item_ts:
            return None
        row, created = cls.objects.get_or_create(overlay=overlay, channel=channel, item_ts=item_ts)
        return row if created else None
