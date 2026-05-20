"""Ship-time FSM reconciliation (extracted from ``pr.py`` by concern, #748).

The ``pr create`` command walks the ticket FSM to REVIEWED before
``ship()`` so the transition is legal. That reconcile is its own concern
— the single source of truth (#694) is the phase ledger, or on the
user-authorized ``--skip-validation`` path the authorization itself —
and is kept here so ``pr.py`` stays within the module-health LOC bar and
the reconcile rule has one self-documenting home.
"""

import contextlib

from django.db import transaction
from django_fsm import TransitionNotAllowed

from teatree.core.models import Ticket

# States where the ship reconcile is a no-op: already shippable
# (``REVIEWED``) or genuinely post-ship-success (``SHIPPED``/``MERGED``/
# ``DELIVERED``). ``IN_REVIEW`` is intentionally NOT here (#798): a ticket
# stranded at ``IN_REVIEW`` by a failed/incomplete prior ship whose phase
# chain is split across sessions must reconcile ``IN_REVIEW → REVIEWED``
# so ``ship()`` can re-fire — otherwise ``pr create`` returns the
# ``{'allowed': False, 'missing': []}`` deadlock. ``reconcile_reviewed``
# now lists ``IN_REVIEW`` as a legal source; the ``suppress`` below remains
# defence-in-depth so any non-source state still degrades to the caller's
# structured failure rather than a raw raise.
_SHIP_RECONCILE_NOOP_STATES = frozenset(
    {
        Ticket.State.REVIEWED,
        Ticket.State.SHIPPED,
        Ticket.State.MERGED,
        Ticket.State.DELIVERED,
    }
)


def reconcile_fsm_for_ship(ticket: Ticket, *, consume_reviewing_tasks: bool = False) -> None:
    """Walk the FSM to REVIEWED so ``ship()`` is legal (#694, #748, #1118).

    Called after a passing gate AND on the ``--skip-validation`` path
    (the user-authorized attestation substitute, so the FSM follows the
    authorization — /t3:ship §5 #2). At/beyond REVIEWED the FSM walk
    is a no-op (those states are not legal ``reconcile_reviewed``
    sources, so a post-ship re-entry would raise raw
    ``TransitionNotAllowed``). The ``suppress`` is defence-in-depth so
    a future source-set change still degrades to the caller's
    structured failure, never a raw raise. Rationale: BLUEPRINT §4.3 +
    the ``reconcile_reviewed`` FSM-table row.

    #1118: when called from the gate-verified path
    (``_check_shipping_gate``), pass ``consume_reviewing_tasks=True``
    so the orphan PENDING/CLAIMED reviewing task is drained — same
    side effect as ``review()``. The drain runs even when the FSM walk
    is a no-op (already at REVIEWED), because a prior ungated path
    (``ticket transition reconcile_reviewed``, ``--skip-validation``)
    could have left the FSM at REVIEWED with the reviewing task still
    PENDING. The skip-validation path leaves it ``False`` (the user's
    authorization substitutes for the gate but not for the per-task
    attestation contract; the loop's orphan sweep handles those tasks
    on its own clock).
    """
    with transaction.atomic(), contextlib.suppress(TransitionNotAllowed):
        if ticket.state not in _SHIP_RECONCILE_NOOP_STATES:
            ticket.reconcile_reviewed()
            ticket.save()
        if consume_reviewing_tasks:
            ticket._consume_pending_phase_tasks("reviewing")  # noqa: SLF001
