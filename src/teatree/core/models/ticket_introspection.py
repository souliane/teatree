from typing import TYPE_CHECKING

from django.apps import apps

from teatree.core.models.ticket_data import TicketFacet
from teatree.core.models.ticket_number import derive_issue_number
from teatree.core.models.ticket_worktree_checks import worktree_has_commits_ahead

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket
    from teatree.core.models.ticket_artifacts import PortResolver, TicketArtifacts


class TicketIntrospectionModel(TicketFacet):
    """Read-only identity, liveness, and diff/artifact introspection over the ticket and its related rows."""

    class Meta:
        abstract = True

    def has_active_work(self) -> bool:
        """True iff this ticket has an open session or an active (pending/claimed) task.

        The single owner of the ticket-liveness rule the reapers and the relocate
        command consult — a busy ticket must never be torn down.
        """
        if self.sessions.filter(ended_at__isnull=True).exists():  # type: ignore[attr-defined]  # Django reverse FK
            return True
        # apps.get_model, not a direct import: task.py imports ticket.py at module scope (real cycle).
        task_model = apps.get_model("core", "Task")
        return self.tasks.filter(status__in=task_model.Status.active()).exists()  # type: ignore[attr-defined]  # Django reverse FK

    @property
    def is_terminal(self) -> bool:
        """True when the ticket is in a genuinely terminal/abandoned state (SHIPPED/MERGED/DELIVERED/IGNORED)."""
        return self.state in self._TERMINAL_STATES

    def may_expedite(self) -> bool:
        """True iff this ticket may carry a human-authorized PENDING-checks waiver (PR-07).

        The flag alone grants NO merge bypass — it only makes the per-CLEAR,
        SHA-bound waiver ISSUABLE (§17.4.3 / ``MergeClear.expedite_pending_waived_by``).
        """
        return self.expedited

    @property
    def ticket_number(self) -> str:
        """Forge issue number derived from ``issue_url``, else the pk (see ``derive_issue_number``).

        Denormalized into the indexed ``issue_number`` column for O(1) resolves;
        this property keeps the pk fallback for rows carrying no forge number.
        """
        return derive_issue_number(self.issue_url) or str(self.pk)

    def has_shippable_diff(self) -> bool:
        """Return True iff at least one worktree has commits ahead of its base branch.

        Used by ``review()`` to skip auto-scheduling shipping when there is
        nothing to ship — typically meta-tracker tickets whose work already
        landed via sibling PRs. Manual ``schedule_shipping()`` callers are not
        gated.
        """
        worktree_model = apps.get_model("core", "Worktree")
        return any(worktree_has_commits_ahead(wt) for wt in worktree_model.objects.filter(ticket=self))

    def artifacts(self: "Ticket", *, port_resolver: "PortResolver | None" = None) -> "TicketArtifacts":
        """Read-only artifact-discovery aggregation (#273) — see ``ticket_artifacts``."""
        from teatree.core.models.ticket_artifacts import collect_ticket_artifacts  # noqa: PLC0415 — import cycle

        return collect_ticket_artifacts(self, port_resolver=port_resolver)
