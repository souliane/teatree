"""Idempotency ledger for Slack review-broadcast scans (#1131).

A broadcast in a review channel is one Slack message that names one or more
MR/PR URLs. The :class:`ScannedBroadcast` row is the durable record that
"this broadcast (channel, ts) has already been classified and dispatched"
— the scanner can re-scan the channel safely because the unique constraint
on ``(channel, slack_ts)`` deduplicates and the stored classification lets
follow-up ticks observe what's already been done.

The lifecycle mirrors :class:`ReviewAssignment`: one row per
``(channel, slack_ts)``, monotonic state transitions, reviewer-task linkage,
no double dispatch while a reviewer task covers the row.

Idempotency is not the emission gate
------------------------------------

The ledger answers "have I seen this broadcast?"; ``reviewer_task_id``
answers "did a reviewer actually get dispatched for it?". Gating emission on
the former makes a broadcast reviewable exactly once ever — so a dispatch lost
to a dead worker, a failed task, or an exhausted budget leaves that review
permanently unreachable. :attr:`ScannedBroadcast.awaiting_reviewer_dispatch`
is the emission gate; the unique constraint remains the idempotency key.

State machine
-------------

``all_merged`` — every MR in the broadcast is already merged + approved.
The scanner posts ``:white_check_mark:`` on the parent message and skips
reviewer dispatch (per ``feedback_review_request_already_done_green_check``).

``pending`` — at least one MR is still open. The scanner posts ``:eyes:`` and
dispatches a reviewer task scoped to the open subset.

``mixed`` is folded into ``pending`` — the only behavioural distinction the
broadcast scanner needs is "is there work left for the reviewer?" Once the
last open MR closes, a later scan flips the row to ``all_merged`` and
re-reacts ``:white_check_mark:``.

Sticky manual flips (#1320)
---------------------------

``manually_classified`` is the operator-applied override that survives
rescans. The auto-derived ``_classify()`` only inspects MR state + approval;
other skip signals (my_notes on the MR, non-self ``:eyes:`` / ``:white_check_mark:``
reactions, author=me, upvotes) are not encoded in that decision and would
silently revert any direct ``classification='all_merged'`` write on the next
``t3 loop tick``. :meth:`mark_manually_classified` pins the verdict and the
flag; :meth:`record` no-ops on sticky rows so the operator's flip is durable.
"""

from dataclasses import dataclass
from typing import ClassVar

from django.db import models
from django.utils import timezone


@dataclass(frozen=True, slots=True)
class BroadcastObservation:
    """Origin metadata for a :class:`ScannedBroadcast` row.

    The ``(channel, slack_ts)`` pair is the idempotency key. ``mr_urls``
    captures every MR URL the scanner extracted from the message text;
    ``classification`` records the verdict the scanner reached on this
    tick. ``overlay`` tags the row for multi-overlay deployments.
    """

    channel: str
    slack_ts: str
    mr_urls: list[str]
    classification: str
    overlay: str = ""


class ScannedBroadcast(models.Model):
    """One classification record for a Slack review-broadcast message.

    Rows are uniquely keyed on ``(channel, slack_ts)``. A re-scan of the
    same broadcast is a no-op when the classification has not changed; if
    a previously-pending broadcast now classifies as ``all_merged`` (the
    last open MR closed), the scanner upgrades the row, re-reacts on
    Slack, and clears the reviewer-task pointer.
    """

    class Classification(models.TextChoices):
        ALL_MERGED = "all_merged"
        PENDING = "pending"

    overlay = models.CharField(max_length=64, blank=True, default="")
    channel = models.CharField(max_length=64)
    slack_ts = models.CharField(max_length=64)
    mr_urls = models.JSONField(default=list)
    classification = models.CharField(max_length=16, choices=Classification.choices)
    reviewer_task_id = models.CharField(max_length=64, blank=True, default="")
    observed_at = models.DateTimeField(default=timezone.now)
    reclassified_at = models.DateTimeField(null=True, blank=True)
    manually_classified = models.BooleanField(default=False)
    manually_classified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_scanned_broadcast"
        ordering: ClassVar = ["observed_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["channel", "slack_ts"],
                name="uniq_scannedbroadcast_channel_ts",
            ),
        ]

    def __str__(self) -> str:
        return f"scanned-broadcast<{self.pk}:{self.classification} {self.channel}/{self.slack_ts}>"

    @classmethod
    def record(cls, observation: BroadcastObservation) -> "ScannedBroadcast | None":
        """Insert one row idempotently on ``(channel, slack_ts)``.

        Returns the new row on first observation, and again on any later
        observation the caller still has work for: a changed classification
        (pending → all_merged once the last open MR closes) or a pending
        broadcast no reviewer task covers. ``None`` means there is nothing
        left to do for this broadcast.
        """
        if not observation.channel or not observation.slack_ts:
            return None
        row, created = cls.objects.get_or_create(
            channel=observation.channel,
            slack_ts=observation.slack_ts,
            defaults={
                "overlay": observation.overlay,
                "mr_urls": list(observation.mr_urls),
                "classification": observation.classification,
            },
        )
        if created:
            return row
        if row.manually_classified:
            # #1320: an operator-applied skip-signal (my_notes, non-self reaction,
            # author=me, upvotes) is durable. The scanner's auto-derived verdict
            # does not override a row marked manual — only an explicit reset does.
            return None
        if row.classification == observation.classification:
            return row if row.awaiting_reviewer_dispatch else None
        row.classification = observation.classification
        row.mr_urls = list(observation.mr_urls)
        row.reclassified_at = timezone.now()
        row.reviewer_task_id = ""
        row.save(update_fields=["classification", "mr_urls", "reclassified_at", "reviewer_task_id"])
        return row

    def mark_manually_classified(self, classification: "ScannedBroadcast.Classification | str") -> bool:
        """Pin *classification* on this row and mark it sticky against rescans (#1320).

        Operators (or sub-agents) call this when a broadcast is socially
        "done" — my_notes, non-self reactions claiming the MR, author=me,
        upvotes — even though the auto-derived classifier would still emit
        ``pending``. The flag survives subsequent ``record`` calls so the
        next ``t3 loop tick`` does not revert the verdict. Idempotent:
        re-marking with the same classification is a no-op and returns
        ``False``.
        """
        value = classification.value if isinstance(classification, self.Classification) else str(classification)
        if self.manually_classified and self.classification == value:
            return False
        updated = (
            type(self)
            .objects.filter(pk=self.pk)
            .update(
                classification=value,
                manually_classified=True,
                manually_classified_at=timezone.now(),
            )
        )
        if updated:
            self.refresh_from_db(fields=["classification", "manually_classified", "manually_classified_at"])
        return bool(updated)

    @property
    def awaiting_reviewer_dispatch(self) -> bool:
        """True when this pending broadcast has no reviewer task covering it.

        Having *seen* a broadcast before is idempotency, not coverage — the
        review only became reachable if a reviewer task was actually created.
        A row whose task was never recorded, was deleted, or FAILED lost its
        emission and must be re-emitted. A COMPLETED task still counts as
        covered: the review happened, and the broadcast signal carries no head
        SHA for the downstream at-head dedup to key on, so re-emitting on it
        would re-review on every tick.
        """
        if self.classification != self.Classification.PENDING:
            return False
        if not self.reviewer_task_id.isdigit():
            return True
        from teatree.core.models.task import Task  # noqa: PLC0415 — lazy: avoids the models import cycle

        return not Task.objects.filter(pk=self.reviewer_task_id).exclude(status=Task.Status.FAILED).exists()

    def attach_reviewer_task(self, task_id: str) -> bool:
        """Persist the dispatched reviewer-task id on this broadcast row.

        Idempotent: re-attaching the same id is a no-op. Used by the
        scanner to keep the audit trail "broadcast → reviewer task"
        traversable from the ledger.
        """
        if not task_id or self.reviewer_task_id == task_id:
            return False
        self.reviewer_task_id = task_id
        self.save(update_fields=["reviewer_task_id"])
        return True
