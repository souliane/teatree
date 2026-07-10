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
        from teatree.instance_id import instance_id  # noqa: PLC0415 — leaf import kept out of module load

        kw.setdefault("claimed_by_instance", instance_id())
        row, created = self.get_or_create(issue_url=issue_url, overlay=overlay, defaults=kw)
        return row if created else None

    def cache_from_fleet_claim(
        self, issue_url: str, overlay: str, *, claim_ref_sha: str, claimed_by_instance: str
    ) -> "ImplementedIssueMarker":
        """Record the marker as a CACHE of a won fleet claim ref (Stage 2).

        Called by the issue-implementer dispatch AFTER the cross-instance mutex
        (``teatree.core.fleet.claim``) granted the ref, so exactly-once was already
        enforced by the server. Unlike :meth:`claim` (which returns ``None`` on a
        pre-existing row), this always returns a usable marker stamped with the
        fencing sha the ship gate re-verifies — the ref is the authority, the row
        its cache. The fleet wiring lives in the dispatch layer, not here, so the
        model layer keeps no dependency on the higher ``teatree.core`` coordination
        modules.
        """
        row, _created = self.update_or_create(
            issue_url=issue_url,
            overlay=overlay,
            defaults={"claim_ref_sha": claim_ref_sha, "claimed_by_instance": claimed_by_instance},
        )
        return row

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
    #: The fleet ``instance_id`` that claimed this issue. The row is per-instance,
    #: so this names the owner for cross-instance reconciliation and is what
    #: Stage 2's GitHub claim refs fence on.
    claimed_by_instance = models.CharField(max_length=64, blank=True, default="")
    #: The Stage 2 fleet-claim fencing token: the sha of the commit
    #: ``refs/teatree/claims/<slug>`` points at when this instance won the mutex.
    #: Empty when the claim was granted local-only (kill-switch OFF). The ship
    #: fence re-reads the ref and refuses the outward write if it no longer points
    #: here (the claim was stolen).
    claim_ref_sha = models.CharField(max_length=64, blank=True, default="")

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
