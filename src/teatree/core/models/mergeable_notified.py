"""Idempotency ledger for the mergeable-awaiting-review DM (#2568 sibling).

When ``PrSweepScanner`` finds a COLLEAGUE-FACING own PR that is CI-green,
not-draft, not-conflicted, and not-behind but has no actionable
``MergeClear`` row, it cannot auto-merge — a colleague review is the gate.
Instead of a silent ``no_clear_for_head`` skip, the sweep emits a
flag-level signal that DMs the user the MR link + "mergeable, ready to
request review."

This ledger keeps that DM at exactly once per head: ``record`` inserts a
row keyed on the unique ``(slug, pr_id, head_sha)`` and returns ``None``
on a dup, so re-ticking on the same head produces no second DM. A new
push (new head SHA) records a fresh row and re-fires exactly one DM.

Mirrors :class:`teatree.core.models.scanned_failed_e2e.ScannedFailedE2E`
and :class:`teatree.core.models.auto_review_dispatch.AutoReviewDispatch`
(insert-once ``record`` keyed on a per-head unique constraint).
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class MergeableNotified(models.Model):
    """One ``(slug, pr_id, head_sha)`` mergeable-DM observation row.

    The unique constraint ``(slug, pr_id, head_sha)`` is the dedup gate:
    the mergeable DM fires the first time the sweep sees a head and never
    again for that head; a new commit (new head SHA) re-arms exactly one
    new DM.
    """

    overlay = models.CharField(max_length=64, blank=True, default="")
    slug = models.CharField(max_length=255)
    pr_id = models.IntegerField()
    head_sha = models.CharField(max_length=64)
    pr_url = models.URLField(max_length=512, blank=True, default="")
    notified_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_mergeable_notified"
        ordering: ClassVar = ["-notified_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["slug", "pr_id", "head_sha"],
                name="uniq_mergeable_notified_slug_pr_head",
            ),
        ]

    def __str__(self) -> str:
        return f"mergeable-notified<{self.pk}:{self.slug}#{self.pr_id}@{self.head_sha[:8]}>"

    @classmethod
    def record(
        cls,
        *,
        slug: str,
        pr_id: int,
        head_sha: str,
        pr_url: str = "",
        overlay: str = "",
    ) -> "MergeableNotified | None":
        """Insert idempotently; return the new row on first sight, ``None`` on dup."""
        if not slug or not head_sha:
            return None
        row, created = cls.objects.get_or_create(
            slug=slug,
            pr_id=pr_id,
            head_sha=head_sha,
            defaults={"pr_url": pr_url, "overlay": overlay},
        )
        return row if created else None
