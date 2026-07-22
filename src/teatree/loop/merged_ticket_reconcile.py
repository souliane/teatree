"""Advance a ticket stranded in a pre-merged FSM state whose PR already merged (#3540).

The #3540 wedge: an author-role ticket entered via a NON-LADDER phase
(``debugging``/``bughunt``/``retro`` produce no work-state — they are absent from
``Ticket._PHASE_PRODUCES_STATE``) whose PR merged OUTSIDE the keystone sits at its
entry state (``NOT_STARTED`` in the incident) forever. Every automatic exit is
closed at once: the two ``NOT_STARTED`` -> terminal hatches
(``mark_reviewed_externally`` / ``mark_review_no_action``) are reviewer-only, the
author ladder's ``review()`` requires ``TESTED``, and ``reconcile_merged`` — which
DOES accept every pre-merged state for ANY role — is only ever driven by the merge
keystone (``merge.execution.record_merge_and_advance``). An out-of-keystone merge
therefore never reconciles the ticket, stranding it until a human transitions it.

This idempotent tick pass closes it structurally: every ticket in a pre-merged,
non-terminal state whose linked ``PullRequest`` row is MERGED is driven through
``reconcile_merged()`` — role- and phase-agnostic, so a ``debugging``-only author
ticket exits like any other. The ``merge_evidence`` gate still guards the
transition (a keystone ``MergeAudit`` row OR a live, fail-closed forge probe), so a
row that is not really merged never advances. Per-row isolation: one poison ticket
is logged and skipped, never aborting the sweep.

Lives in ``teatree.loop`` (orchestration): it composes the ``core`` FSM transition
with the merged ``PullRequest`` rows the tick already reconciled, run right after
:mod:`teatree.loop.manual_pr_reconcile` so a row flipped to MERGED this same tick
reconciles its ticket without waiting for the next one.
"""

import logging
from typing import TYPE_CHECKING

from django_fsm import can_proceed

from teatree.core.models.errors import InvalidTransitionError

if TYPE_CHECKING:
    from teatree.core.models import Ticket

logger = logging.getLogger(__name__)


def reconcile_merged_tickets() -> int:
    """Drive every ticket stranded in a pre-merged state with a merged PR to MERGED.

    Returns the number of tickets advanced. Idempotent — a re-run finds the
    already-MERGED tickets excluded, so it changes nothing.
    """
    count = 0
    for ticket in _stranded_tickets():
        try:
            if _reconcile_one(ticket):
                count += 1
        except InvalidTransitionError as exc:
            # The merge_evidence gate refused (no MergeAudit row and the forge could
            # not confirm the merge). Fail-closed by design — the PR row is not
            # trustworthy evidence on its own — so this is a quiet skip, not an error.
            logger.debug("Merged-ticket reconcile skipped ticket %s: %s", ticket.pk, exc)
        except Exception:
            logger.exception("Merged-ticket reconcile skipped ticket %s after an unexpected error", ticket.pk)
    return count


def _stranded_tickets() -> list["Ticket"]:
    from teatree.core.models import PullRequest, Ticket  # noqa: PLC0415 — ORM import needs the app registry

    # Every ticket with a merged PR row that has not yet reached MERGED. MERGED is
    # excluded to skip the keystone-idempotent self-transition (re-firing the gate
    # buys nothing); the remaining ``reconcile_merged`` source membership — which
    # pointedly refuses the post-merged/abandoned states (RETROSPECTED/DELIVERED/
    # IGNORED) so the FSM is never dragged backward — is enforced per row by the
    # ``can_proceed`` guard in :func:`_reconcile_one`, so the source list stays the
    # FSM's single source of truth rather than a copy that could drift.
    return list(
        Ticket.objects.filter(pull_requests__state=PullRequest.State.MERGED)
        .exclude(state=Ticket.State.MERGED)
        .distinct()
    )


def _reconcile_one(ticket: "Ticket") -> bool:
    if not can_proceed(ticket.reconcile_merged):
        return False
    ticket.reconcile_merged()
    ticket.save()
    logger.info(
        "Reconciled ticket %s to MERGED — PR merged out-of-keystone left it stranded (#3540)",
        ticket.pk,
    )
    return True
