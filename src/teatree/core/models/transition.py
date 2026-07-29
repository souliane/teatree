from typing import ClassVar

from django.db import models

from teatree.core.models.ticket import Ticket


class TicketTransitionQuerySet(models.QuerySet):
    def state_edges(self) -> "TicketTransitionQuerySet":
        """The rows that record an actual move — the ticket's history.

        A reopened ticket (``reopen`` / ``reopen_for_followup`` / ``rework``) wants to
        know how it got where it is, so an edge is kept for as long as the ticket row
        exists. Measured on the live box: ~410 edges in total, against 3.2M rows. They
        cost nothing; nothing prunes them.
        """
        return self.exclude(from_state=models.F("to_state"))

    def excluding_each_tickets_boundary(self) -> "TicketTransitionQuerySet":
        """Drop each ticket's earliest and latest transition from the candidate set.

        The earliest is the ticket's creation proxy: ``Ticket`` carries no creation
        timestamp, so ``factory_signal_queries._fix_tickets_created_in`` dates a fix
        ticket by ``Min(created_at)`` over its transitions — pruning it moves that
        minimum FORWARD and can date an old ticket into the current S2 window. The
        latest is the ticket's last-activity signal, which the stale-ticket and
        stuck-redispatch scanners read as ``Max(created_at)``. Both cost one row.
        """
        by_recency = self.model.objects.filter(ticket_id=models.OuterRef("ticket_id"))
        return self.annotate(
            _earliest_pk=models.Subquery(by_recency.order_by("created_at", "pk").values("pk")[:1]),
            _latest_pk=models.Subquery(by_recency.order_by("-created_at", "-pk").values("pk")[:1]),
        ).exclude(models.Q(pk=models.F("_earliest_pk")) | models.Q(pk=models.F("_latest_pk")))

    def prunable(self) -> "TicketTransitionQuerySet":
        """Transitions safe to delete (#3871) — decided per row, not per table.

        A ``from_state == to_state`` row records NO edge: the move it describes did not
        happen. Such a row is the only thing this lane touches, so a reopened ticket
        still has its complete history (:meth:`state_edges`) — the test that proves it
        is the deliverable here, not the deletion.

        The trigger is the ticket CLOSING, not a row aging: a closed ticket's
        operational residue is dead weight the moment it closes, and waiting out a
        window is arbitrary. Terminal is ``Ticket.marker_release_states()`` plus
        RETROSPECTED — the same set the sibling ``TaskAttempt`` lane resolves through,
        never a second hand-rolled list. An open ticket's rows are never touched.

        ``django.core.signals`` stopped writing these rows at the source (#3876), so
        this lane is the standing backstop for the residue and for any writer that
        guard does not cover — not the primary remedy.
        """
        finished = Ticket.marker_release_states() | {Ticket.State.RETROSPECTED}
        return self.filter(
            ticket__state__in=finished, from_state=models.F("to_state")
        ).excluding_each_tickets_boundary()


class TicketTransition(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="transitions")
    session = models.ForeignKey(
        "core.Session",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transitions",
    )
    from_state = models.CharField(max_length=32)
    to_state = models.CharField(max_length=32)
    triggered_by = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TicketTransitionQuerySet.as_manager()

    class Meta:
        db_table = "teatree_tickettransition"
        ordering: ClassVar = ["created_at"]
        # The table shipped with only the two single-column FK indexes Django adds, so
        # ``created_at`` was unindexed on 3.2M rows: every window read (``standup``,
        # ``checking``, the factory signals) full-scanned, and the prune's per-ticket
        # boundary probe degraded to a scan of that ticket's whole trail per candidate
        # row — quadratic, and measured not to finish. ``(ticket, created_at)`` turns
        # both into index seeks; the bare ``created_at`` index serves the
        # ticket-agnostic window readers.
        indexes: ClassVar = [
            models.Index(fields=["ticket", "created_at"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.from_state} → {self.to_state} ({self.triggered_by})"
