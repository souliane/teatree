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
