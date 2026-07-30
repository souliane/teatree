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

from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils import timezone

from teatree.core.modelkit.notify_policy import OWNER_AUDIENCE_VALUES


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
        # A fallback direct send that returned a message ts (so the DM almost
        # certainly landed) but whose round-trip verification read could not
        # confirm it. Terminal and NON-recoverable: it must never trigger a
        # re-post — the message was sent, only the confirmation read failed —
        # so a retry stands down rather than double-posting (#1181 race).
        SENT_UNVERIFIED = "sent_unverified", "Sent (unconfirmed round-trip read)"
        NOOP = "noop", "Noop (no backend)"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired (re-delivery abandoned)"
        # An INTERNAL-audience notification: logged + terminally recorded, never
        # DM'd and never re-delivered. The deny-by-default counterpart to SENT.
        LOGGED = "logged", "Logged (internal audience, never DM'd)"

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

    # A recoverable INFO row gets at most this many re-delivery attempts
    # before it is terminally EXPIRED. The cross-tick drain bumps
    # ``attempts`` each tick a row is attempted but does not deliver; once
    # the cap is reached the row is abandoned so it can never grind every
    # tick forever (#2064 — the no-backend NOOP that re-records under the
    # same key and re-queues indefinitely).
    MAX_REDELIVERY_ATTEMPTS: ClassVar = 5

    # An operator notification, not a message queue: a stranded INFO DM
    # older than this is stale noise, not an in-flight delivery. The drain
    # terminally EXPIRES it WITHOUT delivery so a weeks-old notification can
    # never surface in the user's DM (the worse failure of #2063 if the
    # re-claim path ever started working).
    REDELIVERY_AGE_CUTOFF: ClassVar = timedelta(hours=72)

    idempotency_key = models.CharField(max_length=255, unique=True)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(max_length=16, choices=Status.choices)
    # Notification-relevance audience (:mod:`teatree.core.modelkit.notify_policy`). Only
    # owner-audience rows are re-delivered; INTERNAL / empty (pre-migration) rows
    # are terminally expired. Blank for rows written before the policy landed.
    audience = models.CharField(max_length=32, blank=True, default="")
    text = models.TextField()
    channel_ref = models.CharField(max_length=255, blank=True)
    posted_ts = models.CharField(max_length=64, blank=True)
    permalink = models.URLField(max_length=512, blank=True)
    error_message = models.TextField(blank=True)
    transport = models.CharField(max_length=16, choices=Transport.choices, blank=True, default="")
    attempts = models.PositiveIntegerField(default=0)
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

        Excludes rows that must never re-deliver again (#2064): a row whose
        :attr:`attempts` reached :attr:`MAX_REDELIVERY_ATTEMPTS`, and a row
        older than :attr:`REDELIVERY_AGE_CUTOFF` (stale operator noise). Both
        are terminally EXPIRED by :meth:`expire_stale_info` rather than retried
        forever. A terminal EXPIRED/SENT row is excluded by status. A fresh
        SENDING row is a genuine in-flight delivery and is excluded.

        Only rows whose :attr:`audience` the owner actually reads (the
        :data:`~teatree.core.modelkit.notify_policy.OWNER_AUDIENCE_VALUES` set) are
        re-deliverable: an INTERNAL row is a log-only notification and a
        pre-migration row (blank ``audience``) is unclassified, so both are
        excluded here and terminally expired by :meth:`expire_stale_info`
        rather than surfacing late in the owner's DM.
        """
        moment = timezone.now()
        stale_before = moment - cls.SENDING_STALE_AFTER
        age_cutoff = moment - cls.REDELIVERY_AGE_CUTOFF
        terminal = Q(status__in=tuple(cls._RECOVERABLE))
        stale_claim = Q(status=cls.Status.SENDING, posted_at__lte=stale_before)
        return (
            cls.objects.filter(kind=cls.Kind.INFO, audience__in=OWNER_AUDIENCE_VALUES)
            .filter(terminal | stale_claim)
            .filter(posted_at__gt=age_cutoff, attempts__lt=cls.MAX_REDELIVERY_ATTEMPTS)
            .order_by("posted_at", "pk")[:limit]
        )

    @classmethod
    def expire_stale_info(cls, *, now: datetime | None = None) -> int:
        """Terminally EXPIRE recoverable INFO rows that must never re-deliver.

        Three cases, all abandoned WITHOUT a delivery attempt: a row older than
        :attr:`REDELIVERY_AGE_CUTOFF` (a weeks-stale operator notification that
        must never reach the user's DM late), a row whose :attr:`attempts`
        reached :attr:`MAX_REDELIVERY_ATTEMPTS` (the bound that stops unbounded
        per-tick grinding when the backend never resolves, #2064), and a
        NON-owner-audience row — an INTERNAL notification or a pre-migration row
        with a blank ``audience`` — which is log-only and must never be
        re-delivered regardless of age.

        Idempotent: only rows in the recoverable set (NOOP/FAILED/stale-SENDING,
        INFO kind) are touched, and the update is a single conditional write, so
        a second pass over an already-EXPIRED row is a no-op. Returns the number
        of rows expired.
        """
        moment = now or timezone.now()
        stale_before = moment - cls.SENDING_STALE_AFTER
        age_cutoff = moment - cls.REDELIVERY_AGE_CUTOFF
        terminal = Q(status__in=tuple(cls._RECOVERABLE))
        stale_claim = Q(status=cls.Status.SENDING, posted_at__lte=stale_before)
        too_old = Q(posted_at__lte=age_cutoff)
        too_many = Q(attempts__gte=cls.MAX_REDELIVERY_ATTEMPTS)
        not_owner = ~Q(audience__in=OWNER_AUDIENCE_VALUES)
        return (
            cls.objects.filter(kind=cls.Kind.INFO)
            .filter(terminal | stale_claim)
            .filter(too_old | too_many | not_owner)
            .update(status=cls.Status.EXPIRED)
        )

    @classmethod
    def bump_attempt(cls, idempotency_key: str) -> None:
        """Record one consumed re-delivery attempt that did not deliver.

        Incremented by the drain when a recoverable row was attempted but did
        not deliver (the backend did not resolve, or a configured send failed).
        Keyed by ``idempotency_key`` rather than pk because a failed delivery
        through :meth:`claim_delivery` deletes the recoverable row and recreates
        a fresh one under the same key — the pk changes but the key does not.
        :meth:`claim_delivery` carries the prior ``attempts`` onto the recreated
        row so this bump lands on the accumulated count, not a reset-to-zero row;
        without that the failed-delivery path would re-record at ``attempts=1``
        every tick and the :attr:`MAX_REDELIVERY_ATTEMPTS` bound would never trip
        (#2068). Drives that bound so a row that can never deliver is EXPIRED
        rather than retried forever. An ``F`` expression so concurrent drains
        never lose a count.
        """
        cls.objects.filter(idempotency_key=idempotency_key).update(attempts=models.F("attempts") + 1)

    @classmethod
    def record_logged(cls, idempotency_key: str, *, kind: str, text: str, audience: str) -> None:
        """Terminally record an INTERNAL-audience notification — logged, never DM'd.

        The deny-by-default sink for :attr:`~teatree.core.modelkit.notify_policy.NotifyAudience.INTERNAL`
        notifications: :func:`teatree.core.notify.notify_user` short-circuits BEFORE
        any backend resolution and calls this so a durable LOGGED audit row exists
        without a DM. Idempotent on ``idempotency_key`` (``get_or_create``) so a
        per-tick re-flag of the same signal never accumulates rows.
        """
        try:
            with transaction.atomic():
                cls.objects.get_or_create(
                    idempotency_key=idempotency_key,
                    defaults={
                        "kind": kind,
                        "status": cls.Status.LOGGED,
                        "text": text,
                        "audience": audience,
                    },
                )
        except IntegrityError:
            pass

    @classmethod
    def claim_delivery(
        cls,
        idempotency_key: str,
        *,
        kind: str,
        text: str,
        audience: str = "",
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
        SENDING claim so a retry still re-delivers; its ``attempts`` count is
        carried onto the fresh row so the :attr:`MAX_REDELIVERY_ATTEMPTS` bound
        accumulates across delete-recreate cycles rather than resetting (#2068).
        """
        manager = cls.objects.using(using) if using else cls.objects
        with transaction.atomic(using=using):
            row = manager.select_for_update().filter(idempotency_key=idempotency_key).first()
            prior_attempts = 0
            if row is not None:
                if row.status == cls.Status.SENT:
                    return DeliveryClaim.ALREADY_SENT
                recoverable = row.status in cls._RECOVERABLE or cls.is_stale_sending(row.status, row.posted_at)
                if not recoverable:
                    return DeliveryClaim.IN_FLIGHT
                prior_attempts = row.attempts
                row.delete()
            manager.create(
                idempotency_key=idempotency_key,
                kind=kind,
                status=cls.Status.SENDING,
                text=text,
                audience=audience,
                attempts=prior_attempts,
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
