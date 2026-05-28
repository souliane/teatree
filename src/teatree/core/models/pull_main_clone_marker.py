"""Per-repo last-pull-at ledger for the pull-main-clone scanner.

The :class:`PullMainCloneScanner` runs every loop tick but only issues
``git fetch`` / ``git pull --ff-only`` against a work-repo main clone
when the cadence has elapsed. ``PullMainCloneMarker`` is the durable
record that carries the cadence gate across tick boundaries — without
it a short tick cadence (e.g. 30s) would degenerate into a 30s
git-fetch cadence and spam each work repo's origin on every machine
running the loop.

This is the sibling of :class:`SelfUpdateMarker`: that one gates the
fast-forward of the editable *teatree+overlay* clones, this one gates
the fast-forward of the *work-repo* main clones under
``$T3_WORKSPACE_DIR`` (the clones a feature worktree is created from).
Two ledgers because the two scanners walk disjoint clone sets on
independent cadences.

One row per ``repo_label``. The scanner upserts the row after each
pass (success or skip) so the next tick can short-circuit cheaply by
reading the ``last_pull_at`` column instead of shelling out to git.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class PullMainCloneMarker(models.Model):
    """One ``(repo_label)`` cadence marker for a work-repo main clone.

    ``repo_label`` is the stable identity the scanner uses to find a
    clone's cadence row. Because the same repo basename can belong to
    more than one overlay, the wiring layer namespaces the label with
    the overlay name (``"<overlay>:<repo>"``) so two overlays sharing a
    repo basename keep independent cadence ledgers. The filesystem path
    is recorded on ``repo_path`` for diagnostics but is not part of the
    identity.

    ``last_outcome`` is the human-readable verdict the scanner reached
    on the most recent pass — ``updated`` / ``up_to_date`` / ``skipped``
    / ``failed`` — and is mirrored in the emitted :class:`ScanSignal`'s
    ``kind`` field. ``last_pulled_sha`` is the post-pass HEAD SHA so a
    follow-up tick can detect drift even when the scanner itself reports
    ``up_to_date``.
    """

    repo_label = models.CharField(max_length=128, unique=True)
    repo_path = models.CharField(max_length=512, blank=True, default="")
    last_outcome = models.CharField(max_length=16, blank=True, default="")
    last_reason = models.CharField(max_length=200, blank=True, default="")
    last_pulled_sha = models.CharField(max_length=64, blank=True, default="")
    last_pull_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_pull_main_clone_marker"
        ordering: ClassVar = ["-last_pull_at"]

    def __str__(self) -> str:
        return f"pull-main-clone<{self.repo_label}:{self.last_outcome}@{self.last_pull_at.isoformat()}>"
