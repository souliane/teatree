"""Ledger of one pull-request head whose records followed the patch from an earlier head (#5450).

A verdict, critic row, anti-vacuity attestation and merge clearance are each bound to one head, so a
merge of ``main`` into the branch moves the head and strands all four although the PR's patch is the
same. Deciding that is :func:`teatree.core.merge.head_carry_over.carry_over_head`'s job; this row is
its explicit record, naming both heads and the evidence, one per ``(slug, pr_id, to_head)``. A
REFUSED row is kept too: it is what stops the forge being asked again about the same head, and what
tells the sweep a head came from a changed patch.
"""

from collections.abc import Iterable
from typing import ClassVar

from django.db import models
from django.utils import timezone

from teatree.core.models.branch_update_attempt import BranchUpdateAttempt


class HeadCarryOverManager(models.Manager["HeadCarryOver"]):
    def decided(self, *, slug: str, pr_id: int, head: str) -> "HeadCarryOver | None":
        return self.filter(slug__iexact=slug.strip(), pr_id=pr_id, to_head=head.strip().lower()).first()

    def carried_from(self, *, slug: str, pr_id: int, head: str, from_heads: Iterable[str]) -> bool:
        """Whether *head* arrived from one of *from_heads* with the patch unchanged."""
        return self.filter(
            slug__iexact=slug.strip(),
            pr_id=pr_id,
            to_head=head.strip().lower(),
            outcome=HeadCarryOver.Outcome.CARRIED,
            from_head__in=[sha.strip().lower() for sha in from_heads],
        ).exists()

    def left_patch_unchanged_by_update(self, *, slug: str, pr_id: int, head: str) -> bool:
        """Whether the sweep's own branch update produced *head* from a head with the same patch."""
        updated = BranchUpdateAttempt.objects.filter(slug__iexact=slug.strip(), pr_id=pr_id)
        return self.carried_from(
            slug=slug, pr_id=pr_id, head=head, from_heads=updated.values_list("head_sha", flat=True)
        )


class HeadCarryOver(models.Model):
    class Outcome(models.TextChoices):
        CARRIED = "carried", "Carried"
        REFUSED = "refused", "Refused"

    slug = models.CharField(max_length=255)
    pr_id = models.IntegerField()
    from_head = models.CharField(max_length=64)
    to_head = models.CharField(max_length=64)
    outcome = models.CharField(max_length=8, choices=Outcome.choices)
    reason = models.CharField(max_length=32, blank=True, default="")
    patch_id = models.CharField(max_length=64, blank=True, default="")
    carried = models.JSONField(default=dict, blank=True)
    recorded_at = models.DateTimeField(default=timezone.now)

    objects: ClassVar[HeadCarryOverManager] = HeadCarryOverManager()

    class Meta:
        db_table = "teatree_head_carry_over"
        ordering: ClassVar = ["-recorded_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["slug", "pr_id", "to_head"], name="uniq_head_carry_over_slug_pr_head"),
        ]

    def __str__(self) -> str:
        return f"head-carry-over<{self.slug}#{self.pr_id} {self.from_head[:8]}->{self.to_head[:8]} {self.outcome}>"
