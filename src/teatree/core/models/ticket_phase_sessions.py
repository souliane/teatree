from typing import TYPE_CHECKING

from django.db import transaction

from teatree.core.models.ticket_data import TicketFacet

if TYPE_CHECKING:
    from teatree.core.models.session import Session


class TicketPhaseSessionModel(TicketFacet):
    """The canonical phase-visit session resolution and cross-session phase ledger (#694, #801)."""

    class Meta:
        abstract = True

    def aggregate_phase_records(self) -> tuple[list[str], dict[str, dict[str, str]]]:
        """Union the phase records across all of this ticket's sessions (#694).

        Returns ``(visited_phases, phase_visits)`` merged across
        ``self.sessions`` in creation order. ``visited_phases`` is a
        de-duplicated list; ``phase_visits`` keeps the first recorded
        ``agent_id`` per phase (earliest session wins) as a deterministic
        audit trail of who recorded each phase — it is not consumed for
        gate enforcement. The shipping gate consumes the ``visited_phases``
        union because FSM-advancing ``visit-phase`` forks fresh sessions by
        design — the required phases are legitimately scattered, and the
        single source of truth is the ticket's lifecycle, not one session.
        """
        visited: list[str] = []
        visits: dict[str, dict[str, str]] = {}
        for session in self.sessions.order_by("pk"):  # ty: ignore[unresolved-attribute]
            for phase in session.visited_phases or []:
                if phase not in visited:
                    visited.append(phase)
            for phase, record in (session.phase_visits or {}).items():
                if phase not in visits:
                    visits[phase] = record
        return visited, visits

    def resolve_phase_session(self, *, agent_id: str = "loop") -> "Session":
        """The single canonical phase-visit session for the attestation writers (#801).

        Which ``Session`` a phase visit lands on was decided four
        inconsistent ways (``ensure_session`` earliest+locked; the
        ``lifecycle visit-phase`` CLI, the ``tasks`` phase-handoff
        command each ``order_by("-pk")`` *latest* with an unlocked raw
        blank-``agent_id`` create on miss; the ``pr`` gate *latest* as
        its gate object). A CLI visit then wrote the *latest* session
        while dispatch reused the *earliest*, splitting attestation
        across sessions (#801). The three attestation writers now route
        here; the read-only gate uses :meth:`find_phase_session`.

        Policy: the **earliest** session (``order_by("pk")`` — the one
        dispatch's attestation uses, so the ledger never splits),
        selected/created inside one ``transaction.atomic()`` with the
        ticket row ``select_for_update``-locked (dispatch callers have
        no surrounding transaction, so concurrent loop ticks for the
        same ``issue_url`` must serialise). Always returns a Session —
        on miss it creates one with a guaranteed **non-blank**
        ``agent_id`` (never the raw blank-``agent_id`` create that left
        the ``phase_visits`` audit trail unattributed).
        """
        from teatree.core.models.session import Session  # noqa: PLC0415 — import cycle

        with transaction.atomic():
            type(self).objects.select_for_update().filter(pk=self.pk).first()
            existing = self.sessions.order_by("pk").first()  # ty: ignore[unresolved-attribute]
            if existing is not None:
                return existing
            return Session.objects.create(ticket=self, agent_id=agent_id.strip() or "loop")

    def find_phase_session(self) -> "Session | None":
        """Read-only canonical phase-visit session for the gate (#801).

        Same earliest + ticket-row-locked selection policy as
        :meth:`resolve_phase_session` but **never creates** — a gate
        check must not have the side effect of minting a session.
        """
        with transaction.atomic():
            type(self).objects.select_for_update().filter(pk=self.pk).first()
            return self.sessions.order_by("pk").first()  # ty: ignore[unresolved-attribute]

    def ensure_session(self, *, agent_id: str = "loop") -> "Session":
        """Durable phase-attestation Session for this ticket (#748).

        Thin alias of the canonical :meth:`resolve_phase_session` (#801
        SSOT) — kept for its existing callers / API.
        """
        return self.resolve_phase_session(agent_id=agent_id)
