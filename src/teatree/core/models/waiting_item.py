"""Manual entries for the durable waiting-on-you lane (PR-21, M7).

The auto-populated waiting entries — unresolved questions, PRs awaiting a
merge authorization, pending review requests — are computed live from their
own durable sources (:mod:`teatree.core.waiting`), so resolving the underlying
thing clears the entry by construction and no sync state is duplicated. This
model carries the ONE thing those sources cannot see: a free-text item the
operator jots down themselves ("chase the finance sign-off"), open until they
explicitly resolve it.

The shape mirrors the manual half of :class:`teatree.core.models.known_issue.KnownIssue`
— a guarded ``add`` factory that refuses empty text, an ``open`` predicate the
gatherer and CLI share, and a single-use ``resolve``.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class WaitingItemError(ValueError):
    """A :class:`WaitingItem` was rejected at add time — empty text."""


class WaitingItemManager(models.Manager["WaitingItem"]):
    """Add / resolve / open surface for the manual waiting-lane entries."""

    def open(self) -> models.QuerySet["WaitingItem"]:
        """Every unresolved row, oldest first (the set the lane counts)."""
        return self.filter(resolved_at__isnull=True).order_by("created_at")

    def add(self, text: str) -> "WaitingItem":
        """Record one operator-authored waiting item; refuse empty text."""
        clean = text.strip()
        if not clean:
            msg = "waiting item text is required and must be non-empty (PR-21)"
            raise WaitingItemError(msg)
        return self.create(text=clean)

    def resolve(self, item_id: int) -> bool:
        """Mark an open item resolved by pk single-use; ``False`` when absent/resolved."""
        updated = self.filter(pk=item_id, resolved_at__isnull=True).update(resolved_at=timezone.now())
        return updated > 0


class WaitingItem(models.Model):
    """One durable operator-authored "waiting on you" entry."""

    text = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)

    objects: ClassVar[WaitingItemManager] = WaitingItemManager()

    class Meta:
        db_table = "teatree_waiting_item"
        ordering: ClassVar = ["created_at"]

    def __str__(self) -> str:
        return f"waiting-item<{self.pk}:{'open' if self.is_open else 'resolved'} '{self.text[:40]}'>"

    @property
    def is_open(self) -> bool:
        """True while the item is unresolved."""
        return self.resolved_at is None
