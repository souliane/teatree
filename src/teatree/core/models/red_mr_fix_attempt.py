"""Idempotency ledger for red-MR auto-fix dispatches (#1295 capability D).

When a ``my_pr.failed`` or ``my_pr.conflicted`` signal is dispatched to the
``t3:debug`` agent the loop records a :class:`RedMrFixAttempt` row keyed on
``(pr_url, head_sha, kind)``. Re-ticking on the same head must not re-dispatch —
the row is the gate the dispatcher / scanner consults before emitting a new agent
action.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class RedMrFixAttempt(models.Model):
    """One auto-fix dispatch attempt for a merge request's head SHA.

    The unique key ``(pr_url, head_sha, kind)`` deduplicates re-ticks: a second
    tick on the same head returns the existing row and the dispatcher skips the
    agent invocation. When the PR moves to a new head (force-push, new commit) a
    fresh row records the new attempt — the agent runs again only on genuinely
    new breakage.

    ``kind`` separates the two ways a merge request can be un-mergeable, because
    they are independent conditions with independent remedies: CI can be red on a
    head that merges cleanly, and a head can conflict with main while CI is green.
    One ledger keyed on the pair keeps a conflict fix from consuming the CI fix's
    slot (and the reverse) while both share the head-scoped dedupe.
    """

    class Kind(models.TextChoices):
        CI_RED = "ci_red", "CI red"
        MERGE_CONFLICT = "merge_conflict", "Merge conflict"

    pr_url = models.URLField(max_length=512)
    head_sha = models.CharField(max_length=64)
    kind = models.CharField(max_length=32, choices=Kind.choices, default=Kind.CI_RED)
    overlay = models.CharField(max_length=64, blank=True, default="")
    dispatched_at = models.DateTimeField(default=timezone.now)
    worktree_hint = models.CharField(max_length=512, blank=True, default="")

    class Meta:
        db_table = "teatree_red_mr_fix_attempt"
        ordering: ClassVar = ["-dispatched_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["pr_url", "head_sha", "kind"],
                name="uniq_redmrfix_url_sha_kind",
            ),
        ]

    def __str__(self) -> str:
        return f"red-mr-fix<{self.pk}:{self.kind}:{self.pr_url}@{self.head_sha[:8]}>"

    @classmethod
    def claim(
        cls,
        *,
        pr_url: str,
        head_sha: str,
        kind: "str | RedMrFixAttempt.Kind" = Kind.CI_RED,
        overlay: str = "",
        worktree_hint: str = "",
    ) -> "RedMrFixAttempt | None":
        """Insert a row idempotently; return the new row or ``None`` on dup.

        ``None`` means "already dispatched for this ``(pr_url, head_sha, kind)``
        on a previous tick — do not re-dispatch."
        """
        if not pr_url or not head_sha:
            return None
        row, created = cls.objects.get_or_create(
            pr_url=pr_url,
            head_sha=head_sha,
            kind=kind,
            defaults={"overlay": overlay, "worktree_hint": worktree_hint},
        )
        return row if created else None
