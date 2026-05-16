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

# States at or beyond REVIEWED: nothing to reconcile, and NOT legal
# ``reconcile_reviewed`` sources — ``reconcile_fsm_for_ship`` no-ops here
# so a post-ship re-entry cannot raise raw ``TransitionNotAllowed``.
_SHIP_RECONCILE_NOOP_STATES = frozenset(
    {
        Ticket.State.REVIEWED,
        Ticket.State.SHIPPED,
        Ticket.State.IN_REVIEW,
        Ticket.State.MERGED,
        Ticket.State.DELIVERED,
    }
)


def reconcile_fsm_for_ship(ticket: Ticket) -> None:
    """Walk the FSM to REVIEWED so ``ship()`` is legal (#694, #748).

    Called after a passing gate AND on the ``--skip-validation`` path
    (the user-authorized attestation substitute, so the FSM follows the
    authorization — /t3:ship §5 #2). No-op at/beyond REVIEWED: those
    states are not legal ``reconcile_reviewed`` sources, so a post-ship
    re-entry (flaky-push retry / second bootstrap) would raise raw
    ``TransitionNotAllowed``. The ``suppress`` is defence-in-depth so a
    future source-set change still degrades to the caller's structured
    failure, never a raw raise. Rationale: BLUEPRINT §4.3 + the
    ``reconcile_reviewed`` FSM-table row.
    """
    if ticket.state in _SHIP_RECONCILE_NOOP_STATES:
        return
    with transaction.atomic(), contextlib.suppress(TransitionNotAllowed):
        ticket.reconcile_reviewed()
        ticket.save()
