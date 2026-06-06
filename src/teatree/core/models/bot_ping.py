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
from datetime import datetime, timedelta
from typing import ClassVar

from django.db import models, transaction
from django.db.models import Q
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

    # A SENDING row is a delivery claim held while the DM is in flight. A
    # crash between claim and finalize strands it; left forever-SENDING it
    # would block every later same-key call (day-granular keys like
    # ``loops_tick_errors:{utc_day}`` are reused all day). A SENDING row
    # older than this is treated as a crashed claim and becomes recoverable;
    # a fresher one is a genuine concurrent in-flight delivery and still
    # blocks. Comfortably exceeds any real single delivery (sub-second) while
    # staying well under the loop tick interval so recovery lands next tick.
    SENDING_STALE_AFTER: ClassVar = timedelta(seconds=300)

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
    def is_stale_sending(cls, status: str, posted_at: datetime, *, now: datetime | None = None) -> bool:
        """Whether a row is a STALE SENDING claim (a crashed, abandonable claim).

        The single source of truth for the staleness rule, shared by
        :meth:`claim_delivery` and the fallback's recoverability check so the
        rule can never drift between them. ``True`` iff the row is SENDING and
        older than :attr:`SENDING_STALE_AFTER` — its owner crashed before
        finalizing. A fresher SENDING is a genuine concurrent in-flight
        delivery and still blocks. Non-SENDING statuses are never "stale
        sending".
        """
        if status != cls.Status.SENDING:
            return False
        moment = now or timezone.now()
        return posted_at <= moment - cls.SENDING_STALE_AFTER

    @classmethod
    def recoverable_info(cls, *, limit: int = 50) -> "models.QuerySet[BotPing]":
        """INFO rows that never delivered and can be re-attempted on a later tick.

        The durable backlog the cross-tick re-delivery drain (:func:`teatree.
        core.notify.drain_undelivered_notifies`) consumes. A bot→user INFO DM
        fired from a sub-agent shell with no reachable backend (``pass`` /
        ``gpg`` unavailable in the restricted sub-agent PATH) lands as a NOOP
        row; a configured backend whose send broke lands as FAILED; a claim
        whose owner crashed before finalizing strands a SENDING row. All three
        are re-deliverable once a tick runs in a context with a working backend
        — that is exactly the recoverability set :meth:`claim_delivery` already
        replaces, scoped here to the INFO kind (QUESTION rows are drained
        separately by :func:`drain_deferred_questions`).

        A fresh SENDING row is a genuine in-flight delivery and is excluded.
        """
        moment = timezone.now()
        stale_before = moment - cls.SENDING_STALE_AFTER
        terminal = Q(status__in=tuple(cls._RECOVERABLE))
        stale_claim = Q(status=cls.Status.SENDING, posted_at__lte=stale_before)
        return cls.objects.filter(kind=cls.Kind.INFO).filter(terminal | stale_claim).order_by("posted_at", "pk")[:limit]

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
        no-ops, the idempotent success); ``IN_FLIGHT`` — a fresh SENDING claim
        held by a concurrent tick (the caller stands down, no DM); ``CLAIMED``
        — this caller won and must deliver, then finalize the SENDING row via
        :meth:`finalize_sent` / :meth:`finalize_failed`. A recoverable row
        (FAILED/NOOP — #1306, or a STALE SENDING whose owner crashed before
        finalizing — see :meth:`is_stale_sending`) is replaced by the fresh
        SENDING claim so a retry still re-delivers.
        """
        manager = cls.objects.using(using) if using else cls.objects
        with transaction.atomic(using=using):
            row = manager.select_for_update().filter(idempotency_key=idempotency_key).first()
            if row is not None:
                if row.status == cls.Status.SENT:
                    return DeliveryClaim.ALREADY_SENT
                recoverable = row.status in cls._RECOVERABLE or cls.is_stale_sending(row.status, row.posted_at)
                if not recoverable:
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
