"""Per-session phase-ledger retirement — the workstream-boundary reset (#1286).

Split out of the (module-LOC-capped) ``Ticket`` model so the god-model sheds a
self-contained helper. ``Ticket.reopen()`` is the explicit workstream boundary;
it retires the prior workstream's phase attestations so the next workstream
re-earns them from scratch (the sanctioned ``lifecycle clear-ledger --confirm``
performs the same reset).
"""

from typing import TYPE_CHECKING

from django.db import transaction

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


def retire_phase_ledger(ticket: "Ticket") -> None:
    """Retire every session's phase ledger for *ticket* (#1286).

    Mirrors ``lifecycle clear-ledger --confirm``: a per-session reset of
    ``visited_phases``, ``phase_visits``, ``repos_modified``, ``repos_tested`` so
    the next workstream re-earns its attestations. Wrapped in
    ``transaction.atomic`` so ``select_for_update`` works even when the FSM caller
    (the loop ``reopen_ticket`` mechanical path) has no surrounding transaction.
    """
    with transaction.atomic():
        for session in ticket.sessions.select_for_update().all():  # type: ignore[attr-defined]  # Django reverse FK
            session.visited_phases = []
            session.phase_visits = {}
            session.repos_modified = []
            session.repos_tested = []
            session.save(
                update_fields=["visited_phases", "phase_visits", "repos_modified", "repos_tested"],
            )
