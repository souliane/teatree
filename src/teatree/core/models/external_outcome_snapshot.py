"""Append-only ledger of forge-read external outcomes (#4506).

One row per forge read of "what actually reached ``main`` in the trailing
window". Every other factory measure is derived from teatree's own bookkeeping —
tasks ``completed``, phases visited, tickets advanced — all of which stay green
while nothing merges. This table is the counterweight: its numbers come from the
forge, so no amount of internal success can move them.

The row is also the cache. A forge read costs a network round trip per repo and
``t3 doctor`` runs many times a day, so the reader serves any snapshot younger
than its TTL and only then goes to the network.

``status`` keeps a failed read distinguishable from a genuine zero: a row is
written only for a read that COMPLETED, so "no snapshot" and "zero merges" can
never be confused for each other downstream.
"""

import datetime as dt
from typing import TYPE_CHECKING, ClassVar

from django.db import models
from django.utils import timezone

if TYPE_CHECKING:
    from teatree.core.factory.external_outcomes import ExternalOutcomes


class ExternalOutcomeSnapshotManager(models.Manager["ExternalOutcomeSnapshot"]):
    def record(self, outcomes: "ExternalOutcomes", *, overlay: str = "") -> "ExternalOutcomeSnapshot":
        """Persist one completed forge read (the only writer)."""
        return self.create(
            overlay=overlay,
            window_days=outcomes.window_days,
            generated_at=outcomes.generated_at,
            status=outcomes.status.value,
            repo_slugs=list(outcomes.repo_slugs),
            merged_pr_count=len(outcomes.merged_prs),
            merged_pr_refs=[ref.to_dict() for ref in outcomes.merged_prs],
        )

    def latest_fresh(
        self,
        *,
        overlay: str = "",
        ttl: dt.timedelta,
        now: dt.datetime | None = None,
    ) -> "ExternalOutcomeSnapshot | None":
        """The most recent snapshot younger than *ttl*, or ``None`` if the read is due."""
        moment = now or timezone.now()
        return self.filter(overlay=overlay, generated_at__gt=moment - ttl).order_by("-generated_at", "-pk").first()


class ExternalOutcomeSnapshot(models.Model):
    """One forge read of the merges that landed in the trailing window."""

    generated_at = models.DateTimeField(default=timezone.now)
    overlay = models.CharField(max_length=64, blank=True, default="")
    window_days = models.IntegerField(default=0)
    status = models.CharField(max_length=16, default="ok")
    repo_slugs = models.JSONField(default=list, blank=True)
    merged_pr_count = models.IntegerField(default=0)
    merged_pr_refs = models.JSONField(default=list, blank=True)

    objects: ClassVar[ExternalOutcomeSnapshotManager] = ExternalOutcomeSnapshotManager()

    class Meta:
        db_table = "teatree_external_outcome_snapshot"
        ordering: ClassVar = ["-generated_at"]
        indexes: ClassVar = [
            models.Index(fields=["overlay", "generated_at"], name="eos_overlay_generated_idx"),
        ]

    def __str__(self) -> str:
        return f"external-outcome<{self.pk}:{self.status} merged={self.merged_pr_count} window={self.window_days}d>"
