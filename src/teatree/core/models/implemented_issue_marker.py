"""Durable ledger for issue-implementer dispatches (#1549).

When the issue-implementer loop dispatches an issue it records an
:class:`ImplementedIssueMarker` keyed on ``(issue_url, overlay)``. A
re-tick on the same issue finds the existing row and skips re-dispatch,
and the non-terminal row count (``in_flight_count``) is the max-concurrent
budget the loop checks before dispatching the next issue.

Mirrors :class:`teatree.core.models.red_mr_fix_attempt.RedMrFixAttempt`
(idempotent ``claim()`` keyed on a natural identity).
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar

from django.db import models
from django.utils import timezone

#: A maintainer applies this label to withhold an issue from the autonomous
#: factory until they have reviewed it. The issue-implementer claim path
#: filters out any open issue carrying it at selection time — never claimed,
#: never dispatched — regardless of which implementer label it also carries.
NEEDS_TRIAGE_LABEL = "needs-triage"

#: How long a non-terminal marker whose ticket is GONE (no ticket exists for
#: its ``issue_url``) may linger before the reconciler abandons it. A dispatch
#: creates its ticket in the same session, so a marker with no ticket after this
#: window is a stranded claim (the #3100 dispatch-then-drop class), never a
#: legitimately in-flight one. Terminal-ticket markers are released regardless
#: of age — this grace guards only the ticket-gone branch.
_DEFAULT_ORPHAN_GRACE = timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class MarkerReconcileResult:
    """Outcome of one reconcile pass: the marker pks moved to each terminal state."""

    completed: tuple[int, ...] = ()
    abandoned: tuple[int, ...] = ()

    @property
    def released(self) -> int:
        return len(self.completed) + len(self.abandoned)


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
        return self.filter(overlay=overlay).exclude(state__in=ImplementedIssueMarker.State.terminal()).count()

    def find_stale(
        self,
        overlay: str = "",
        *,
        orphan_grace: timedelta | None = None,
    ) -> MarkerReconcileResult:
        """Classify — WITHOUT mutating — which non-terminal markers are reconcilable (#3275).

        A marker is stale when its linked ticket (matched by ``issue_url``, the
        canonical unique key the release signal also keys on) has reached a
        terminal state (``Ticket.marker_release_states()`` → COMPLETED), or when
        no ticket exists for its issue at all and it has outlived
        ``orphan_grace`` (a stranded claim → ABANDONED). ``overlay=""`` spans
        every overlay. The doctor jam-signature check reads this preview; the
        loop and CLI mutate via :meth:`reconcile_stale`.
        """
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415 — peer model, deferred to avoid load-time cycle

        grace = _DEFAULT_ORPHAN_GRACE if orphan_grace is None else orphan_grace
        terminal_states = Ticket.marker_release_states()
        cutoff = timezone.now() - grace
        non_terminal = self.exclude(state__in=ImplementedIssueMarker.State.terminal())
        if overlay:
            non_terminal = non_terminal.filter(overlay=overlay)

        completed: list[int] = []
        abandoned: list[int] = []
        for marker in non_terminal.iterator():
            if not marker.issue_url:
                continue
            ticket_state = Ticket.objects.filter(issue_url=marker.issue_url).values_list("state", flat=True).first()
            if ticket_state is not None:
                if ticket_state in terminal_states:
                    completed.append(marker.pk)
            elif marker.dispatched_at <= cutoff:
                abandoned.append(marker.pk)
        return MarkerReconcileResult(completed=tuple(completed), abandoned=tuple(abandoned))

    def reconcile_stale(
        self,
        overlay: str = "",
        *,
        orphan_grace: timedelta | None = None,
    ) -> MarkerReconcileResult:
        """Release stale markers so the in-flight budget self-heals (#3275).

        Terminal-ticket markers → COMPLETED, gone-ticket orphans past the grace
        → ABANDONED (mirroring the give-up semantics ABANDONED already carries).
        Idempotent: a second pass finds the just-released rows terminal and is a
        no-op. Returns the same :class:`MarkerReconcileResult`
        :meth:`find_stale` computes.
        """
        result = self.find_stale(overlay, orphan_grace=orphan_grace)
        if result.completed:
            self.filter(pk__in=result.completed).update(state=ImplementedIssueMarker.State.COMPLETED)
        if result.abandoned:
            self.filter(pk__in=result.abandoned).update(state=ImplementedIssueMarker.State.ABANDONED)
        return result


class ImplementedIssueMarker(models.Model):
    class State(models.TextChoices):
        DISPATCHED = "dispatched", "Dispatched"
        TICKET_CREATED = "ticket_created", "Ticket created"
        #: The ticket shipped and merged/delivered (or was ignored) — released
        #: from the in-flight budget on ticket completion (the release-on-completion
        #: the lifecycle previously lacked). Distinct from ABANDONED, which stays
        #: reserved for give-up / fleet-claim-steal semantics.
        COMPLETED = "completed", "Completed"
        ABANDONED = "abandoned", "Abandoned"

        @classmethod
        def terminal(cls) -> tuple[str, ...]:
            """States that no longer consume the max-concurrent budget."""
            return (cls.COMPLETED, cls.ABANDONED)

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
