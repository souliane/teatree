"""Durable acquisition queue for ``max_concurrent_local_stacks`` (souliane/teatree#44, #2190).

When ``t3 <overlay> worktree start`` / ``workspace start`` finds no free slot
under the per-overlay cap, the gate (``core/gates/local_stack_gate.py``)
reaps idle stacks, retries, and — if still full — ENQUEUES a row here instead
of exiting 1. A loop scanner (the queue drainer) consults
:meth:`LocalStackQueueItemManager.due_for_attempt` each tick and retries the
acquisition with a Fibonacci-minute backoff (1, 1, 2, 3, 5, 8, 13…). It never
tears down another ticket's stack — it only waits for a slot to free
naturally (a reap, a teardown) and re-fires ``start``.

Statuses: ``QUEUED`` (just enqueued, due immediately) → ``RETRYING`` (a
backoff attempt is scheduled) → ``READY`` (a slot freed and ``start`` was
re-fired) → ``DONE`` (terminal success bookkeeping) or ``DEAD`` (gave up
after ``local_stack_queue_max_attempts``). A partial ``UniqueConstraint``
over ``(worktree)`` while the status is QUEUED/RETRYING makes enqueue
idempotent — re-firing ``start`` against an already-queued worktree returns
the existing row rather than stacking duplicates.
"""

from datetime import datetime, timedelta
from typing import ClassVar

from django.db import models
from django.utils import timezone

from teatree.core.modelkit.fibonacci import fibonacci_minutes
from teatree.core.models.worktree import Worktree


class LocalStackQueueItemQuerySet(models.QuerySet["LocalStackQueueItem"]):
    def active(self) -> "LocalStackQueueItemQuerySet":
        """Rows still competing for a slot (QUEUED or RETRYING)."""
        return self.filter(
            status__in=[LocalStackQueueItem.Status.QUEUED, LocalStackQueueItem.Status.RETRYING],
        )

    def due_for_attempt(self, now: datetime | None = None) -> "LocalStackQueueItemQuerySet":
        """Active rows whose next-attempt time has arrived (drainer's work list).

        A QUEUED row with a null ``next_attempt_at`` is due immediately; a
        RETRYING row is due once ``next_attempt_at <= now``. Ordered oldest
        scheduled first so the drainer is FIFO-fair across waiting tickets.
        """
        moment = now or timezone.now()
        return (
            self.active()
            .filter(models.Q(next_attempt_at__isnull=True) | models.Q(next_attempt_at__lte=moment))
            .order_by("next_attempt_at", "pk")
        )


class LocalStackQueueItemManager(models.Manager["LocalStackQueueItem"]):
    def get_queryset(self) -> LocalStackQueueItemQuerySet:
        return LocalStackQueueItemQuerySet(self.model, using=self._db)

    def active(self) -> LocalStackQueueItemQuerySet:
        return self.get_queryset().active()

    def due_for_attempt(self, now: datetime | None = None) -> LocalStackQueueItemQuerySet:
        return self.get_queryset().due_for_attempt(now)

    def enqueue(self, worktree: Worktree) -> "LocalStackQueueItem":
        """Idempotently enqueue an acquisition request for *worktree*.

        Returns the existing active (QUEUED/RETRYING) row when one is already
        waiting — the partial UniqueConstraint guarantees at most one — so a
        re-fired ``start`` does not stack duplicates. Otherwise creates a
        fresh QUEUED row (due immediately).
        """
        existing = self.active().filter(worktree=worktree).first()
        if existing is not None:
            return existing
        return self.create(overlay=worktree.overlay, worktree=worktree)


class LocalStackQueueItem(models.Model):
    """One queued ``worktree start`` waiting for a local-stack slot to free."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RETRYING = "retrying", "Retrying"
        READY = "ready", "Ready"
        DONE = "done", "Done"
        DEAD = "dead", "Dead"

    overlay = models.CharField(max_length=255)
    worktree = models.ForeignKey(
        Worktree,
        on_delete=models.CASCADE,
        related_name="stack_queue_items",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    objects: ClassVar[LocalStackQueueItemManager] = LocalStackQueueItemManager()

    class Meta:
        db_table = "teatree_local_stack_queue_item"
        ordering: ClassVar = ["next_attempt_at", "pk"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["worktree"],
                condition=models.Q(status__in=["queued", "retrying"]),
                name="uniq_active_stack_queue_item_per_worktree",
            ),
        ]
        indexes: ClassVar = [
            models.Index(fields=["status", "next_attempt_at"]),
        ]

    def __str__(self) -> str:
        return f"stack-queue<{self.worktree_id}:{self.status}#{self.attempt_count}>"  # ty: ignore[unresolved-attribute]

    def schedule_next_attempt(
        self,
        *,
        error: str = "",
        now: datetime | None = None,
        max_attempts: int = 13,
    ) -> None:
        """Advance to RETRYING with a Fibonacci-minute backoff, or DEAD if exhausted.

        Increments ``attempt_count`` and sets ``next_attempt_at`` to
        ``now + fibonacci_minutes(attempt_count)``. When the new
        ``attempt_count`` exceeds *max_attempts* the row is marked DEAD
        (the drainer gives up and surfaces it) rather than retrying forever.
        """
        moment = now or timezone.now()
        self.attempt_count += 1
        self.error_message = error
        if self.attempt_count > max_attempts:
            self.status = self.Status.DEAD
            self.save(update_fields=["status", "attempt_count", "error_message", "updated_at"])
            return
        self.status = self.Status.RETRYING
        self.next_attempt_at = moment + timedelta(minutes=fibonacci_minutes(self.attempt_count))
        self.save(update_fields=["status", "attempt_count", "next_attempt_at", "error_message", "updated_at"])

    def mark_ready(self) -> None:
        """Record that a slot freed and ``start`` was re-fired for this item."""
        self.status = self.Status.READY
        self.save(update_fields=["status", "updated_at"])

    def mark_done(self) -> None:
        """Terminal success bookkeeping — the acquisition is fully settled."""
        self.status = self.Status.DONE
        self.save(update_fields=["status", "updated_at"])
