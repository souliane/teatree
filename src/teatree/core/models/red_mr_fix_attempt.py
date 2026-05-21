"""Idempotency ledger for red-MR auto-fix dispatches (#1295 capability D).

When a ``my_pr.failed`` signal is dispatched to the ``t3:debug`` agent the
loop records a :class:`RedMrFixAttempt` row keyed on ``(pr_url, head_sha)``.
Re-ticking on the same failing sha must not re-dispatch — the row is the
gate the dispatcher / scanner consults before emitting a new agent action.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class RedMrFixAttempt(models.Model):
    """One auto-fix dispatch attempt for a failing PR head SHA.

    The unique key ``(pr_url, head_sha)`` deduplicates re-ticks: a second
    tick on the same failing SHA returns the existing row and the
    dispatcher skips the agent invocation. When the PR moves to a new
    failing SHA (force-push, new commit) a fresh row records the new
    attempt — the agent runs again only on genuinely new failures.
    """

    pr_url = models.URLField(max_length=512)
    head_sha = models.CharField(max_length=64)
    overlay = models.CharField(max_length=64, blank=True, default="")
    dispatched_at = models.DateTimeField(default=timezone.now)
    worktree_hint = models.CharField(max_length=512, blank=True, default="")

    class Meta:
        db_table = "teatree_red_mr_fix_attempt"
        ordering: ClassVar = ["-dispatched_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["pr_url", "head_sha"],
                name="uniq_redmrfix_url_sha",
            ),
        ]

    def __str__(self) -> str:
        return f"red-mr-fix<{self.pk}:{self.pr_url}@{self.head_sha[:8]}>"

    @classmethod
    def claim(
        cls,
        *,
        pr_url: str,
        head_sha: str,
        overlay: str = "",
        worktree_hint: str = "",
    ) -> "RedMrFixAttempt | None":
        """Insert a row idempotently; return the new row or ``None`` on dup.

        ``None`` means "already dispatched for this ``(pr_url, head_sha)``
        on a previous tick — do not re-dispatch."
        """
        if not pr_url or not head_sha:
            return None
        row, created = cls.objects.get_or_create(
            pr_url=pr_url,
            head_sha=head_sha,
            defaults={"overlay": overlay, "worktree_hint": worktree_hint},
        )
        return row if created else None
