"""Durable Slack-DM-inbound queue (#1014, BLUEPRINT §17.1 invariant 2 / §5.6).

The Slack inbound bridge: a user message DM'd to the overlay bot lands in
this queue as a single :class:`PendingChatInjection` row. The next
``UserPromptSubmit`` handler reads unconsumed rows, formats them as an
``additionalContext`` block, and marks them ``consumed_at``, so the agent
sees them as if the user typed them in Claude Code chat.

Mirrors the :class:`teatree.core.models.deferred_question.DeferredQuestion`
shape — durable, single-use, scoped, idempotent — applied to the *reverse*
direction (user → agent). The Slack ``ts`` is the canonical idempotency
key: the scanner can over-poll safely because ``unique(overlay, ts)``
deduplicates, and the injection handler is safe to re-fire because
``consumed_at`` is stamped once.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class PendingChatInjection(models.Model):
    """One Slack DM from the user waiting to be injected into the next prompt.

    The scanner inserts a row per new message; the ``UserPromptSubmit``
    drain reads unconsumed rows for the loop-owner session, emits them
    into ``additionalContext``, and stamps ``consumed_at`` so a re-fire
    of the hook is a clean no-op.
    """

    overlay = models.CharField(max_length=64, blank=True, default="")
    channel = models.CharField(max_length=64)
    slack_ts = models.CharField(max_length=64)
    user_id = models.CharField(max_length=64, blank=True, default="")
    text = models.TextField()
    received_at = models.DateTimeField(default=timezone.now)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_pending_chat_injection"
        ordering: ClassVar = ["received_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["overlay", "slack_ts"], name="uniq_pendingchat_overlay_ts"),
        ]

    def __str__(self) -> str:
        status = "consumed" if self.consumed_at else "pending"
        return f"pending-chat-injection<{self.pk}:{status} overlay={self.overlay!r} ts={self.slack_ts}>"

    @property
    def is_pending(self) -> bool:
        return self.consumed_at is None

    @classmethod
    def record(
        cls,
        *,
        channel: str,
        slack_ts: str,
        text: str,
        overlay: str = "",
        user_id: str = "",
    ) -> "PendingChatInjection | None":
        """Insert one row idempotently on ``(overlay, slack_ts)``.

        Returns the new row, or ``None`` if a row for this ``(overlay, ts)``
        already exists (the scanner over-polled). The ``ts`` is the
        canonical idempotency key — Slack guarantees uniqueness per
        channel and the scanner only ever sees one channel per overlay.
        """
        if not slack_ts or not channel or not text.strip():
            return None
        row, created = cls.objects.get_or_create(
            overlay=overlay,
            slack_ts=slack_ts,
            defaults={
                "channel": channel,
                "user_id": user_id,
                "text": text,
            },
        )
        return row if created else None

    @classmethod
    def pending(cls, *, overlay: str = "") -> models.QuerySet["PendingChatInjection"]:
        """Return the unconsumed queue for *overlay*, oldest first.

        Pass ``overlay=""`` to drain every overlay's queue (the v1 single-
        overlay path uses ``overlay=""`` consistently and ignores filter).
        """
        qs = cls.objects.filter(consumed_at__isnull=True)
        if overlay:
            qs = qs.filter(overlay=overlay)
        return qs.order_by("received_at")

    def consume(self) -> bool:
        """Mark this row consumed; return ``True`` on the transition, else ``False``.

        Idempotent: a second call on an already-consumed row is a no-op
        and returns ``False``. Returning the transition lets the caller
        emit audit lines only once.
        """
        updated = type(self).objects.filter(pk=self.pk, consumed_at__isnull=True).update(consumed_at=timezone.now())
        if updated:
            self.refresh_from_db(fields=["consumed_at"])
        return bool(updated)
