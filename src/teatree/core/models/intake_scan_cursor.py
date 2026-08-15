"""Where intake's last discovery pass stopped, and whether it finished (#4466).

The scanner walks candidates oldest-filed first under a scan deadline it owns alone. A pass
that ran out of budget used to restart from the oldest candidate on the next tick, so the
newest issues — the only ones that can still become work — were never reached and nothing
recorded that they had been dropped. One row per overlay carries the resume point, so the
next pass continues at the frontier, and the incomplete-pass streak, so ``t3 doctor check``
can say the queue is not draining rather than leaving it to a WARN in the worker log.
"""

import datetime as dt
from typing import ClassVar

from django.db import models
from django.utils import timezone

#: Consecutive incomplete passes before the doctor calls the queue stalled. One short pass is
#: ordinary under load; three in a row means the frontier is not being reached at all.
INCOMPLETE_PASS_ALARM = 3


class IntakeScanCursorManager(models.Manager["IntakeScanCursor"]):
    """Read the resume point, and record what each pass managed to cover."""

    def resume_after(self, overlay: str) -> str:
        """The issue URL an INCOMPLETE last pass stopped on — ``""`` starts at the oldest.

        A pass that FINISHED leaves no resume point, so the next one starts at the oldest
        candidate and the first freed slot goes to the longest-waiting issue (#4238).
        Resuming is what an unfinished pass needs to reach the frontier; carrying it into a
        finished pass would rotate the queue every tick and hand the slot to a newer issue.
        """
        row = self.filter(overlay=overlay).first()
        if row is None or not row.consecutive_incomplete_passes:
            return ""
        return row.last_issue_url

    def record_pass(
        self,
        overlay: str,
        *,
        last_issue_url: str,
        last_issue_created_at: dt.datetime | None = None,
        complete: bool,
        now: dt.datetime | None = None,
    ) -> "IntakeScanCursor":
        """Stamp where the pass stopped; a complete pass clears the incomplete streak."""
        moment = now or timezone.now()
        row, _ = self.get_or_create(overlay=overlay)
        row.last_issue_url = last_issue_url
        row.last_issue_created_at = last_issue_created_at
        if complete:
            row.consecutive_incomplete_passes = 0
            row.last_complete_at = moment
        else:
            row.consecutive_incomplete_passes += 1
            row.last_incomplete_at = moment
        row.save()
        return row

    def stalled(self, *, threshold: int = INCOMPLETE_PASS_ALARM) -> "models.QuerySet[IntakeScanCursor]":
        """Every overlay whose intake has failed to complete a pass *threshold* times running."""
        return self.filter(consecutive_incomplete_passes__gte=threshold).order_by("overlay")


class IntakeScanCursor(models.Model):
    """One overlay's intake resume point and pass health."""

    overlay = models.CharField(max_length=64, blank=True, default="", unique=True)
    #: The last candidate EXAMINED, not the last claimed — the next pass starts after it.
    last_issue_url = models.URLField(max_length=512, blank=True, default="")
    last_issue_created_at = models.DateTimeField(null=True, blank=True)
    consecutive_incomplete_passes = models.PositiveIntegerField(default=0)
    last_incomplete_at = models.DateTimeField(null=True, blank=True)
    last_complete_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects: ClassVar[IntakeScanCursorManager] = IntakeScanCursorManager()

    class Meta:
        verbose_name = "intake scan cursor"

    def __str__(self) -> str:
        return f"{self.overlay or '(global)'} @ {self.last_issue_url or '(start)'}"

    def report(self) -> str:
        """The doctor's one-line account of a stalled intake queue."""
        return (
            f"{self.overlay or '(global)'}: {self.consecutive_incomplete_passes} intake passes in a row ran out of "
            f"budget before finishing; the walk is resuming at {self.last_issue_url or 'the oldest candidate'}"
        )
