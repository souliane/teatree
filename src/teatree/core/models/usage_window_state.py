"""The parked usage-window ledger — one active row per credential lane (Directive #3).

When a Claude usage window empties (the ~5h rolling session limit or the 7-day weekly
limit), the headless dispatch plane used to fold the hit into a terminal FAILED attempt and
go idle forever until a human poked it. This ledger is the durable park state that replaces
that dead-silence: a row records that a lane's window is exhausted and the effective instant
it re-arms, so the ``usage_window_recovery`` loop-timer chain can clear it (and release the
tasks parked behind it) deterministically at the reset instant — no human, no OS cron.

DOMAIN module — like its sibling ``anthropic_token_usage`` it stays free of
``teatree.llm``: the caller (``teatree.agents.usage_window``, which owns the
``LimitCause`` classification) passes the already-resolved effective ``resets_at`` and the
``cause`` string; this model only persists and answers the deterministic
clear/active questions over what it was given.
"""

from datetime import datetime

from django.db import models, transaction
from django.utils import timezone

#: The marker prefix stamped on a parked ``TaskAttempt.error`` so a limit-park reads
#: distinctly from a real failure — the sibling of ``headless._STUCK_LOOP_PREFIX``. Kept
#: here (a DOMAIN home) so the ``teatree.agents`` park recorder AND the
#: ``teatree.core`` repair-loop budget both reference ONE constant: a limit-park is a
#: scheduling event, not a work iteration, so ``task_repair.phase_attempts`` excludes it.
LIMIT_PARKED_PREFIX = "limit_parked: "


class UsageWindowStateQuerySet(models.QuerySet):
    def active(self) -> "UsageWindowStateQuerySet":
        """Uncleared windows — a lane whose exhausted window has not yet re-armed."""
        return self.filter(cleared_at__isnull=True)

    def active_for_lane(self, lane: str) -> "UsageWindowState | None":
        """The uncleared window covering *lane*, or ``None`` — the admission guard's read.

        *lane* is the resolved Layer-2 lane string (``subscription`` / ``metered`` / the
        ambient ``""``); record and admission consult the SAME value so a window recorded
        under a lane is matched by a dispatch on that lane, and the ambient ``""`` key is
        never conflated with an attributed lane.
        """
        return self.active().filter(lane=lane).first()


class UsageWindowState(models.Model):
    """One exhausted usage window on one credential lane — the park-not-fail record."""

    lane = models.CharField(max_length=16, blank=True, default="")
    #: The ``LimitCause`` value string (``subscription_session`` / ``subscription_weekly`` /
    #: ``rate_limit`` / ``api_credit``) — audit + the recovery notification wording.
    cause = models.CharField(max_length=32, blank=True, default="")
    detected_at = models.DateTimeField()
    #: The effective instant the window re-arms — the SDK's structured ``resets_at`` when it
    #: reported one, else ``detected_at + window_horizon(cause)`` computed by the caller.
    #: ``None`` when the cause has no time-based recovery (API-credit exhaustion) — such a
    #: window is never auto-cleared on a timer; the operator must add credits.
    resets_at = models.DateTimeField(null=True, blank=True)
    cleared_at = models.DateTimeField(null=True, blank=True)
    #: How many recovery probes have fired against this row (audit; the recovery chain is
    #: deterministic time-based, so this is diagnostics, not control flow).
    probe_count = models.PositiveIntegerField(default=0)

    objects = UsageWindowStateQuerySet.as_manager()

    class Meta:
        db_table = "teatree_usagewindowstate"

    def __str__(self) -> str:
        state = "cleared" if self.cleared_at else "active"
        return f"usage-window[{self.lane or 'ambient'}/{self.cause}]-{state}"

    def should_clear(self, now: datetime) -> bool:
        """True iff the window has re-armed — the reset instant has passed.

        The deterministic re-arm decision: a window clears exactly once ``now`` reaches its
        effective ``resets_at``. A null ``resets_at`` (credit exhaustion) never clears here.
        """
        return self.resets_at is not None and now >= self.resets_at

    def clear(self, now: datetime) -> None:
        """Stamp the window cleared — it re-armed, so the admission guard stops blocking it."""
        self.cleared_at = now
        self.save(update_fields=["cleared_at"])

    @classmethod
    def record_limit(
        cls,
        *,
        lane: str,
        cause: str,
        resets_at: datetime | None,
        now: datetime | None = None,
    ) -> "UsageWindowState":
        """Record (or refresh) the active window for *lane* — idempotent, one active row per lane.

        A re-detection on a lane that already has an uncleared row UPDATES that row (fresh
        ``cause`` / ``resets_at`` / ``detected_at``) rather than accumulating duplicates, so
        the admission guard and the recovery chain always read a single authoritative window
        per lane. A cleared row is inert — a fresh detection after a clear opens a new row.
        """
        moment = now or timezone.now()
        with transaction.atomic():
            row = cls.objects.active_for_lane(lane)
            if row is None:
                return cls.objects.create(
                    lane=lane,
                    cause=cause,
                    detected_at=moment,
                    resets_at=resets_at,
                )
            row.cause = cause
            row.detected_at = moment
            row.resets_at = resets_at
            row.save(update_fields=["cause", "detected_at", "resets_at"])
            return row
