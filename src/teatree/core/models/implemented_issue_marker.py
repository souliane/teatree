"""Durable ledger for issue-implementer dispatches (#1549).

When the issue-implementer loop dispatches an issue it records an
:class:`ImplementedIssueMarker` keyed on ``(issue_url, overlay)``. A
re-tick on the same issue finds the existing row and skips re-dispatch,
and the non-terminal row count (``in_flight_count``) is the max-concurrent
budget the loop checks before dispatching the next issue.

Mirrors :class:`teatree.core.models.red_mr_fix_attempt.RedMrFixAttempt`
(idempotent ``claim()`` keyed on a natural identity).
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone

#: A maintainer applies this label to withhold an issue from the autonomous
#: factory until they have reviewed it. The issue-implementer claim path
#: filters out any open issue carrying it at selection time — never claimed,
#: never dispatched — regardless of which implementer label it also carries.
NEEDS_TRIAGE_LABEL = "needs-triage"


class ImplementedIssueMarkerManager(models.Manager["ImplementedIssueMarker"]):
    def claim(self, issue_url: str, overlay: str = "", **kw: object) -> "ImplementedIssueMarker | None":
        if not issue_url:
            return None
        row, created = self.get_or_create(issue_url=issue_url, overlay=overlay, defaults=kw)
        return row if created else None

    def in_flight_count(self, overlay: str) -> int:
        return self.filter(overlay=overlay).exclude(state=ImplementedIssueMarker.State.ABANDONED).count()


class ImplementedIssueMarker(models.Model):
    class State(models.TextChoices):
        DISPATCHED = "dispatched", "Dispatched"
        TICKET_CREATED = "ticket_created", "Ticket created"
        ABANDONED = "abandoned", "Abandoned"

    issue_url = models.URLField(max_length=512)
    overlay = models.CharField(max_length=64, blank=True, default="")
    state = models.CharField(max_length=16, choices=State.choices, default=State.DISPATCHED)
    ticket = models.ForeignKey(
        "core.Ticket",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="implemented_issue_markers",
    )
    dispatched_at = models.DateTimeField(default=timezone.now)
    head_sha = models.CharField(max_length=64, blank=True, default="")

    objects: ClassVar[ImplementedIssueMarkerManager] = ImplementedIssueMarkerManager()

    class Meta:
        db_table = "teatree_implemented_issue_marker"
        ordering: ClassVar = ["-dispatched_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["issue_url", "overlay"],
                name="uniq_impl_issue_url_overlay",
            ),
        ]

    def __str__(self) -> str:
        return f"impl-issue<{self.pk}:{self.issue_url}@{self.state}>"
