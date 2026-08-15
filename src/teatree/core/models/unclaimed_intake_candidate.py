"""The durable record of issues intake found admissible but had no budget to claim (#4238).

Intake's own decision is per-tick and log-only, so an issue that keeps losing the race
for a free slot leaves no trace anywhere: the tick reports success, the last-run stamp
advances, and the only way to discover the issue was passed over is to notice a
``Ticket`` row is missing. One row per ``(overlay, issue_url)`` carries when the issue
was filed and when it was FIRST seen waiting, so ``t3 doctor check`` can name it and say
how long it has been waiting.

The ledger is synced wholesale each discovery pass: an issue no longer admissible — it
was claimed, closed, or a ticket now owns it — loses its row, so a row's existence means
"still waiting as of the last pass".
"""

import datetime as dt
from dataclasses import dataclass
from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone

#: How long a candidate must sit admissible-but-unclaimed before the doctor names it.
#: A busy factory passes issues over for hours as a matter of course; a full day means
#: the queue is not draining for that issue and someone should look.
STARVED_AFTER = dt.timedelta(hours=24)

_SECONDS_PER_HOUR = 3600
_HOURS_PER_DAY = 24


@dataclass(frozen=True, slots=True)
class WaitingCandidate:
    """One admissible issue intake could not claim this pass — the manager's write unit."""

    issue_url: str
    title: str = ""
    issue_created_at: dt.datetime | None = None


def _duration_label(delta: dt.timedelta) -> str:
    """Whole days once past a day, whole hours below it — e.g. ``3d`` / ``7h``."""
    hours = int(delta.total_seconds() // _SECONDS_PER_HOUR)
    return f"{hours // _HOURS_PER_DAY}d" if hours >= _HOURS_PER_DAY else f"{hours}h"


class UnclaimedIntakeCandidateManager(models.Manager["UnclaimedIntakeCandidate"]):
    """Sync the per-overlay waiting set, and read the ones that have waited too long."""

    def sync(
        self,
        overlay: str,
        candidates: "list[WaitingCandidate]",
        *,
        now: dt.datetime | None = None,
        complete: bool = True,
    ) -> int:
        """Replace *overlay*'s waiting set with *candidates*, preserving each first sighting.

        Wholesale rather than incremental because a COMPLETE discovery pass sees the WHOLE
        admissible set: an issue absent from *candidates* is no longer waiting, whatever
        the reason, so an incremental upsert would leave rows that outlive their subject.
        Returns the number of rows now waiting.

        ``complete=False`` upserts and evicts NOTHING: a pass that ran out of budget never
        looked at the rest of the queue, so eviction there would delete the witness for
        exactly the starved issues the ledger exists to name (#4466).
        """
        moment = now or timezone.now()
        urls = {candidate.issue_url for candidate in candidates}
        with transaction.atomic():
            if complete:
                self.filter(overlay=overlay).exclude(issue_url__in=urls).delete()
            for candidate in candidates:
                self.update_or_create(
                    overlay=overlay,
                    issue_url=candidate.issue_url,
                    defaults={
                        "title": candidate.title,
                        "issue_created_at": candidate.issue_created_at,
                        "last_seen_at": moment,
                    },
                    create_defaults={
                        "title": candidate.title,
                        "issue_created_at": candidate.issue_created_at,
                        "first_seen_at": moment,
                        "last_seen_at": moment,
                    },
                )
        return len(urls)

    def starved(
        self,
        *,
        threshold: dt.timedelta = STARVED_AFTER,
        now: dt.datetime | None = None,
    ) -> "models.QuerySet[UnclaimedIntakeCandidate]":
        """Every candidate first seen waiting at least *threshold* ago, longest wait first."""
        moment = now or timezone.now()
        return self.filter(first_seen_at__lte=moment - threshold).order_by("first_seen_at")


class UnclaimedIntakeCandidate(models.Model):
    """One issue intake judged admissible and had no budget to claim."""

    overlay = models.CharField(max_length=64, blank=True, default="")
    issue_url = models.URLField(max_length=512)
    title = models.CharField(max_length=512, blank=True, default="")
    #: When the forge says the issue was FILED; null when the payload carried no date.
    issue_created_at = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)

    objects: ClassVar[UnclaimedIntakeCandidateManager] = UnclaimedIntakeCandidateManager()

    class Meta:
        db_table = "teatree_unclaimed_intake_candidate"
        ordering: ClassVar = ["first_seen_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["overlay", "issue_url"],
                name="uniq_unclaimed_intake_candidate",
            ),
        ]

    def __str__(self) -> str:
        return f"unclaimed-intake<{self.overlay}:{self.issue_url}>"

    def waited_label(self, *, now: dt.datetime | None = None) -> str:
        """How long this candidate has been observed waiting, e.g. ``2d``."""
        return _duration_label((now or timezone.now()) - self.first_seen_at)

    def open_label(self, *, now: dt.datetime | None = None) -> str:
        """How long the issue has been OPEN, or ``unknown`` when the forge gave no date."""
        if self.issue_created_at is None:
            return "unknown"
        return _duration_label((now or timezone.now()) - self.issue_created_at)

    def report(self, *, now: dt.datetime | None = None) -> str:
        """The operator-facing line: which issue, how long open, how long passed over."""
        title = self.title or "(untitled)"
        return (
            f"{self.issue_url} ({title}) — admissible but unclaimed for "
            f"{self.waited_label(now=now)}, open {self.open_label(now=now)}"
        )
