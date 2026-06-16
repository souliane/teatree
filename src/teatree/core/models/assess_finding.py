"""Idempotency ledger for ac-reviewing-codebase auto-fix sweep (#1295 capability H).

The loop's review slot's nightly assess sweep enumerates registered
skill repos, runs ``t3 assess run`` against each, and emits one
``skill_drift_detected`` signal per *new* finding. :class:`AssessFinding`
is the dedup ledger keyed on ``(repo, file_path, finding_fingerprint)``;
re-running the sweep on the same repo state produces zero new signals.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class AssessFinding(models.Model):
    """One ``ac-reviewing-codebase`` finding observation.

    ``finding_fingerprint`` is a short hash (or content-derived key) the
    scanner computes from the finding text so unrelated edits to other
    files don't invalidate the dedup. ``repo`` is the absolute path of
    the scanned skill repo. ``file_path`` is the file the finding
    targets (relative to ``repo``). The unique constraint on the triple
    is the gate the sweep consults before dispatching ``t3:coder``.
    """

    overlay = models.CharField(max_length=64, blank=True, default="")
    repo = models.CharField(max_length=512)
    file_path = models.CharField(max_length=512)
    finding_fingerprint = models.CharField(max_length=128)
    severity = models.CharField(max_length=32, blank=True, default="")
    finding_text = models.TextField(blank=True, default="")
    observed_at = models.DateTimeField(default=timezone.now)
    dispatched_task_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "teatree_assess_finding"
        ordering: ClassVar = ["-observed_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["repo", "file_path", "finding_fingerprint"],
                name="uniq_assessfinding_repo_file_fpr",
            ),
        ]

    def __str__(self) -> str:
        return f"assess-finding<{self.pk}:{self.repo}:{self.file_path}>"

    @classmethod
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def record(  # noqa: PLR0913 — model-level ledger record API; each kwarg is a documented field.
        cls,
        *,
        repo: str,
        file_path: str,
        finding_fingerprint: str,
        severity: str = "",
        finding_text: str = "",
        overlay: str = "",
    ) -> "AssessFinding | None":
        """Insert idempotently; return new row or ``None`` on dup."""
        if not repo or not file_path or not finding_fingerprint:
            return None
        row, created = cls.objects.get_or_create(
            repo=repo,
            file_path=file_path,
            finding_fingerprint=finding_fingerprint,
            defaults={
                "overlay": overlay,
                "severity": severity,
                "finding_text": finding_text,
            },
        )
        return row if created else None


class AssessSweepRun(models.Model):
    """Cadence ledger: last assess-sweep run per overlay.

    Capability H's sweep fires AT MOST every
    ``LOOP_REVIEW_ASSESS_INTERVAL_HOURS`` (default 24). One row per
    overlay records the last sweep start; the scanner checks the row's
    age before kicking a new sweep so a fast review-slot cadence doesn't
    cause back-to-back assess runs.
    """

    overlay = models.CharField(max_length=64, blank=True, default="", unique=True)
    last_run_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_assess_sweep_run"

    def __str__(self) -> str:
        return f"assess-sweep<{self.overlay}@{self.last_run_at.isoformat()}>"

    @classmethod
    def is_due(cls, overlay: str, interval_hours: float) -> bool:
        """Return True if no recent run or the latest run is older than the interval."""
        from datetime import timedelta  # noqa: PLC0415

        try:
            row = cls.objects.get(overlay=overlay)
        except cls.DoesNotExist:
            return True
        return (timezone.now() - row.last_run_at) >= timedelta(hours=interval_hours)

    @classmethod
    def mark_run(cls, overlay: str) -> None:
        cls.objects.update_or_create(
            overlay=overlay,
            defaults={"last_run_at": timezone.now()},
        )
