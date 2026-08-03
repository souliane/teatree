"""Idempotency ledger for the sweep's unattended merge-update (#4063).

``PrSweepScanner`` merge-updates a PR whose required checks are red at a STALE
base — the branch is BEHIND its base, so the red verdict judged a base the fix
may already have landed on. ONE attempt per head SHA is the bound the whole
remedy rests on: a successful update mints a new head, so the next tick judges
a fresh verdict at the current base; a FAILED update (conflict, revoked
permission, API error) is never retried at the same head, so a genuinely broken
PR can never be update-looped.

The row is claimed BEFORE the API call, so a crash in the call window forfeits
that head rather than risking a second push. The caller degrades to the
``needs_branch_update`` flag whenever the claim is refused, so a forfeited head
is surfaced rather than dropped, and a new commit re-arms it.

Mirrors :class:`teatree.core.models.mergeable_notified.MergeableNotified`
(insert-once, keyed on a per-head unique constraint).
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class BranchUpdateAttempt(models.Model):
    """One ``(slug, pr_id, head_sha)`` merge-update claim row."""

    overlay = models.CharField(max_length=64, blank=True, default="")
    slug = models.CharField(max_length=255)
    pr_id = models.IntegerField()
    head_sha = models.CharField(max_length=64)
    pr_url = models.URLField(max_length=512, blank=True, default="")
    attempted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_branch_update_attempt"
        ordering: ClassVar = ["-attempted_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["slug", "pr_id", "head_sha"],
                name="uniq_branch_update_attempt_slug_pr_head",
            ),
        ]

    def __str__(self) -> str:
        return f"branch-update-attempt<{self.pk}:{self.slug}#{self.pr_id}@{self.head_sha[:8]}>"

    @classmethod
    def claim(
        cls,
        *,
        slug: str,
        pr_id: int,
        head_sha: str,
        pr_url: str = "",
        overlay: str = "",
    ) -> "BranchUpdateAttempt | None":
        """Claim this head for one update; ``None`` when it is already claimed."""
        if not slug or not head_sha:
            return None
        row, created = cls.objects.get_or_create(
            slug=slug,
            pr_id=pr_id,
            head_sha=head_sha,
            defaults={"pr_url": pr_url, "overlay": overlay},
        )
        return row if created else None
