"""Bot→user Slack notification audit row (#963).

One row per ``notify_user(...)`` call, keyed by ``idempotency_key`` so a
retried turn (same session+turn) does not double-DM. The unique-key
collapse is the same shape as ``DailyDigestMessage``: at-least-once with
happy-path dedup, not exactly-once. Separate model from
``DailyDigestMessage`` because a bot→user notification has no notion of
a daily thread or a per-day root opener — it's a direct DM the agent
issues *to its own operator* outside the active CLI session.

Out of scope: posts made *on the user's behalf* to colleagues/customers
(those route through ``Replier`` / ``ReplyDispatch`` and the on-behalf
gates #960/#949). This model only audits notifications the bot sends
*to* the user.
"""

import enum
from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone


class DeliveryClaim(enum.StrEnum):
    """Outcome of an atomic ``BotPing.claim_delivery`` (the dedup CAS)."""

    CLAIMED = "claimed"
    ALREADY_SENT = "already_sent"
    IN_FLIGHT = "in_flight"


class BotPing(models.Model):
    """One bot→user Slack notification (answer / question / info)."""

    class Kind(models.TextChoices):
        ANSWER = "answer", "Answer"
        QUESTION = "question", "Question"
        INFO = "info", "Info"

    class Status(models.TextChoices):
        SENDING = "sending", "Sending (delivery claimed, in flight)"
        SENT = "sent", "Sent"
        NOOP = "noop", "Noop (no backend)"
        FAILED = "failed", "Failed"

    class Transport(models.TextChoices):
        """Which delivery path actually landed the DM (#1181).

        ``PRIMARY`` is the canonical ``notify_user`` path; ``FALLBACK`` is
        the direct, round-trip-verified messaging-backend send the wrapper
        falls back to when the primary returns ``did not deliver`` (the
        #1173 silent-rc=1 class). ``UNSET`` covers rows written by the
        plain ``notify_user`` egress that does not go through the wrapper.
        """

        UNSET = "", "Unset (direct notify_user)"
        PRIMARY = "primary", "Primary (notify_user)"
        FALLBACK = "fallback", "Fallback (direct verified send)"

    _RECOVERABLE: ClassVar = {Status.FAILED, Status.NOOP}

    idempotency_key = models.CharField(max_length=255, unique=True)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(max_length=16, choices=Status.choices)
    text = models.TextField()
    channel_ref = models.CharField(max_length=255, blank=True)
    posted_ts = models.CharField(max_length=64, blank=True)
    permalink = models.URLField(max_length=512, blank=True)
    error_message = models.TextField(blank=True)
    transport = models.CharField(max_length=16, choices=Transport.choices, blank=True, default="")
    posted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_bot_ping"
        ordering: ClassVar = ["-posted_at"]
        indexes: ClassVar = [
            models.Index(fields=["kind", "posted_at"]),
            models.Index(fields=["status", "posted_at"]),
        ]

    def __str__(self) -> str:
        return f"BotPing[{self.kind}/{self.status}] {self.idempotency_key}"

    @classmethod
    def claim_delivery(
        cls,
        idempotency_key: str,
        *,
        kind: str,
        text: str,
        using: str | None = None,
    ) -> DeliveryClaim:
        """Atomically claim the right to deliver one DM for ``idempotency_key``.

        The dedup CAS that closes the ``notify_user`` double-DM TOCTOU: the
        pre-claim guard was a bare ``filter → first → delete`` that two
        concurrent ticks both passed, then both delivered. This mirrors the
        ``OnBehalfApproval.consume`` / ``LoopLease.acquire`` doctrine — a
        ``select_for_update`` re-read inside one ``transaction.atomic``, so on
        the production SQLite backend (where ``select_for_update`` is a no-op
        and serialization comes from ``transaction_mode=IMMEDIATE``) the
        second tick blocks until the first commits its claim, then observes
        the now-``SENDING`` row and stands down.

        Outcomes: ``ALREADY_SENT`` — a terminal SENT row exists (the caller
        no-ops, the idempotent success); ``IN_FLIGHT`` — another tick already
        claimed delivery (the caller stands down, no DM); ``CLAIMED`` — this
        caller won and must deliver, then finalize the SENDING row via
        :meth:`finalize_sent` / :meth:`finalize_failed`. A prior recoverable
        row (FAILED/NOOP — #1306) is replaced by the fresh SENDING claim so a
        transient-failure retry still re-delivers.
        """
        manager = cls.objects.using(using) if using else cls.objects
        with transaction.atomic(using=using):
            row = manager.select_for_update().filter(idempotency_key=idempotency_key).first()
            if row is not None:
                if row.status == cls.Status.SENT:
                    return DeliveryClaim.ALREADY_SENT
                if row.status not in cls._RECOVERABLE:
                    return DeliveryClaim.IN_FLIGHT
                row.delete()
            manager.create(
                idempotency_key=idempotency_key,
                kind=kind,
                status=cls.Status.SENDING,
                text=text,
            )
        return DeliveryClaim.CLAIMED

    @classmethod
    def finalize_sent(
        cls,
        idempotency_key: str,
        *,
        channel_ref: str,
        posted_ts: str,
        permalink: str,
        using: str | None = None,
    ) -> None:
        """Stamp the claimed SENDING row terminal-SENT after a confirmed delivery."""
        manager = cls.objects.using(using) if using else cls.objects
        manager.filter(idempotency_key=idempotency_key, status=cls.Status.SENDING).update(
            status=cls.Status.SENT,
            channel_ref=channel_ref,
            posted_ts=posted_ts,
            permalink=permalink,
        )

    @classmethod
    def finalize_failed(cls, idempotency_key: str, *, error: str, using: str | None = None) -> None:
        """Stamp the claimed SENDING row terminal-FAILED so a later retry recovers it."""
        manager = cls.objects.using(using) if using else cls.objects
        manager.filter(idempotency_key=idempotency_key, status=cls.Status.SENDING).update(
            status=cls.Status.FAILED,
            error_message=error,
        )
