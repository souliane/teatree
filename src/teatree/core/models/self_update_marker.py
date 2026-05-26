"""Per-repo last-pull-at ledger for the self-update scanner (#1249).

The :class:`SelfUpdateScanner` runs every loop tick but actually issues
``git fetch`` / ``git pull --ff-only`` against each editable clone only
when the cadence has elapsed. ``SelfUpdateMarker`` is the durable record
that carries the cadence gate across tick boundaries — without it, a
short tick cadence (e.g. 30s) would degenerate into a 30s git-fetch
cadence and spam the upstream remote on every machine running the loop.

One row per ``repo_label``. The scanner upserts the row after each pass
(success or skip) so the next tick can short-circuit cheaply by reading
the ``last_pull_at`` column instead of shelling out to git.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class SelfUpdateMarker(models.Model):
    """One ``(repo_label)`` cadence marker.

    ``repo_label`` is the human-friendly tag the scanner uses to identify
    a clone (``"teatree"`` for the core editable clone, the overlay name
    for an overlay clone). It is the unique key — the actual filesystem
    path is recorded on ``repo_path`` for diagnostics but is not part of
    the identity (an editable clone can be moved on disk without
    invalidating the marker, just like the scanner itself doesn't care).

    ``last_outcome`` is the human-readable verdict the scanner reached
    on the most recent pass — ``updated`` / ``up_to_date`` / ``skipped``
    / ``failed`` — and is mirrored in the emitted :class:`ScanSignal`'s
    ``kind`` field. ``last_pulled_sha`` is the post-pass HEAD SHA so a
    follow-up tick can detect drift even when the scanner itself reports
    ``up_to_date``.
    """

    repo_label = models.CharField(max_length=64, unique=True)
    repo_path = models.CharField(max_length=512, blank=True, default="")
    last_outcome = models.CharField(max_length=16, blank=True, default="")
    last_reason = models.CharField(max_length=200, blank=True, default="")
    last_pulled_sha = models.CharField(max_length=64, blank=True, default="")
    last_pull_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_self_update_marker"
        ordering: ClassVar = ["-last_pull_at"]

    def __str__(self) -> str:
        return f"self-update<{self.repo_label}:{self.last_outcome}@{self.last_pull_at.isoformat()}>"
