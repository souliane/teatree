"""Idempotency ledger + claimable-task factory for auto-review dispatch (#68).

When ``PrSweepScanner`` finds an OWN, CI-green, mergeable, non-draft PR on a
full-autonomy overlay with no recorded independent cold-review, it cannot
merge — the maker≠checker boundary forbids self-attestation. Pre-#68 the
scanner only logged ``flag_no_review`` and the PR waited for a human to notice.

This model closes that loop: ``enqueue`` records a row keyed on
``(slug, pr_id, head_sha)`` and creates the claimable ``Task(phase=reviewing)``
the loop self-pump dispatches to ``t3:reviewer``. The reviewer cold-reviews the
PR and records a ``merge_safe`` :class:`ReviewVerdict` bound to the reviewed
head; the NEXT sweep consumes that verdict (``_has_independent_cold_review``)
and merges. Dedup is per ``(PR, head_sha)``: a new push (new head) re-arms
exactly one new task; an open task for the same head never duplicates; a
recorded verdict for the head suppresses enqueue upstream (the PR never reaches
``flag_no_review``).

Mirrors :class:`teatree.core.models.red_mr_fix_attempt.RedMrFixAttempt`
(idempotent claim keyed on ``(pr_url, head_sha)``).
"""

from typing import TYPE_CHECKING, ClassVar

from django.db import models, transaction
from django.utils import timezone

if TYPE_CHECKING:
    from teatree.core.models.task import Task


def build_review_contract(*, slug: str, pr_id: int, head_sha: str, pr_url: str, overlay: str) -> str:
    """The reviewer's standing contract, stamped into the task's execution_reason.

    The dispatched ``t3:reviewer`` reads this to know it must cold-review per
    /t3:review doctrine and RECORD the verdict via the ``review record`` CLI
    bound to the reviewed head SHA — the artifact the next sweep merges on.
    """
    overlay_arg = overlay or "<overlay>"
    return (
        f"Cold-review {pr_url} per /t3:review doctrine, then RECORD the verdict bound to the "
        f"reviewed head SHA so the next pr_sweep can merge it: "
        f"`t3 {overlay_arg} review record {pr_id} {slug} --reviewed-sha {head_sha} "
        f"--reviewer-identity <your-reviewer-id> --verdict merge_safe` (use --verdict hold with "
        f"--findings-json when blocking). The recorded merge_safe ReviewVerdict at head {head_sha[:8]} "
        f"is the artifact pr_sweep consumes to auto-merge this own PR (#68)."
    )


class AutoReviewDispatch(models.Model):
    """One auto-review dispatch for a PR head SHA — the dedup key + task link.

    The unique key ``(slug, pr_id, head_sha)`` deduplicates re-ticks: a second
    sweep on the same head returns the existing row and enqueues no new task.
    A new push (new head SHA) records a fresh row and arms exactly one new
    reviewing task. The ``task`` FK links the dispatch to the claimable
    ``Task(phase=reviewing)`` so the row is a faithful record of what was
    enqueued.
    """

    slug = models.CharField(max_length=255)
    pr_id = models.IntegerField()
    head_sha = models.CharField(max_length=64)
    pr_url = models.URLField(max_length=512, blank=True, default="")
    overlay = models.CharField(max_length=64, blank=True, default="")
    task = models.ForeignKey(
        "core.Task",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="auto_review_dispatches",
    )
    dispatched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_auto_review_dispatch"
        ordering: ClassVar = ["-dispatched_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["slug", "pr_id", "head_sha"],
                name="uniq_auto_review_slug_pr_head",
            ),
        ]

    def __str__(self) -> str:
        return f"auto-review<{self.pk}:{self.slug}#{self.pr_id}@{self.head_sha[:8]}>"

    @classmethod
    def enqueue(
        cls,
        *,
        slug: str,
        pr_id: int,
        head_sha: str,
        pr_url: str = "",
        overlay: str = "",
    ) -> "AutoReviewDispatch | None":
        """Record the dispatch + create one claimable reviewing Task — idempotently.

        Returns the new row (carrying the enqueued task) on first dispatch for
        ``(slug, pr_id, head_sha)``; ``None`` when a row for that head already
        exists (the sweep on a previous tick already armed the review). The row
        insert and the Task creation share one transaction so a row never
        exists without its task and a task is never created without claiming the
        dedup slot.
        """
        if not slug or not head_sha:
            return None
        with transaction.atomic():
            row, created = cls.objects.get_or_create(
                slug=slug,
                pr_id=pr_id,
                head_sha=head_sha,
                defaults={"pr_url": pr_url, "overlay": overlay},
            )
            if not created:
                return None
            task = cls._create_reviewing_task(
                slug=slug,
                pr_id=pr_id,
                head_sha=head_sha,
                pr_url=pr_url,
                overlay=overlay,
            )
            row.task = task
            row.save(update_fields=["task"])
        return row

    @staticmethod
    def _create_reviewing_task(*, slug: str, pr_id: int, head_sha: str, pr_url: str, overlay: str) -> "Task":
        from teatree.core.models.session import Session  # noqa: PLC0415
        from teatree.core.models.task import Task  # noqa: PLC0415
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        ticket, _ = Ticket.objects.get_or_create(
            issue_url=pr_url or f"{slug}#{pr_id}",
            defaults={"overlay": overlay, "role": Ticket.Role.REVIEWER},
        )
        session = Session.objects.create(ticket=ticket, agent_id="auto-review-dispatch")
        return Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason=build_review_contract(
                slug=slug, pr_id=pr_id, head_sha=head_sha, pr_url=pr_url, overlay=overlay
            ),
        )
