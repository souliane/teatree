"""Idempotency ledger for ``/codex:review`` auto-dispatch (#1254).

When :class:`CodexReviewScanner` dispatches ``/codex:review`` for a
newly-pushed PR head SHA, it claims one :class:`CodexReviewMarker` row
keyed on ``(slug, pr_id, head_sha)``. Re-ticking on the same SHA returns
no row and the scanner skips the dispatch — the fleet-of-agents rule is
enforced once per SHA, never on every tick.

A force-push (new SHA) inserts a fresh row and re-fires the review.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class CodexReviewMarker(models.Model):
    """One ``/codex:review`` dispatch record for a PR head SHA.

    The unique key ``(slug, pr_id, head_sha)`` deduplicates re-ticks: a
    second tick on the same head returns no row from :meth:`claim` and
    the scanner skips. When the PR force-pushes a new SHA, a fresh row
    records the new dispatch — the codex review re-fires only on
    genuinely new code.
    """

    slug = models.CharField(max_length=128)
    pr_id = models.IntegerField()
    head_sha = models.CharField(max_length=64)
    overlay = models.CharField(max_length=64, blank=True, default="")
    variant = models.CharField(max_length=64, blank=True, default="")
    dispatched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_codex_review_marker"
        ordering: ClassVar = ["-dispatched_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["slug", "pr_id", "head_sha"],
                name="uniq_codexreviewmarker_slug_pr_sha",
            ),
        ]

    def __str__(self) -> str:
        return f"codex-review-marker<{self.pk}:{self.slug}#{self.pr_id}@{self.head_sha[:8]}>"

    @classmethod
    def claim(
        cls,
        *,
        slug: str,
        pr_id: int,
        head_sha: str,
        overlay: str = "",
        variant: str = "",
    ) -> "CodexReviewMarker | None":
        """Insert a row idempotently; return the new row or ``None`` on dup.

        ``None`` means "already dispatched for this
        ``(slug, pr_id, head_sha)`` on a previous tick — do not
        re-dispatch."
        """
        if not slug or not pr_id or not head_sha:
            return None
        row, created = cls.objects.get_or_create(
            slug=slug,
            pr_id=pr_id,
            head_sha=head_sha,
            defaults={"overlay": overlay, "variant": variant},
        )
        return row if created else None
