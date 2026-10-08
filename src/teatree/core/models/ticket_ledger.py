"""Per-session phase-ledger retirement — the workstream-boundary reset (#1286).

Split out of the (module-LOC-capped) ``Ticket`` model so the god-model sheds a
self-contained helper. ``Ticket.reopen()`` is the explicit workstream boundary;
it retires the prior workstream's phase attestations so the next workstream
re-earns them from scratch (the sanctioned ``lifecycle clear-ledger --confirm``
performs the same reset), and records the merged PRs it reopened over.
"""

from typing import TYPE_CHECKING

from django.db import transaction

from teatree.core.models.pull_request import PullRequest

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from teatree.core.models.ticket import Ticket

_REOPENED_OVER = "reopened_over_pr_urls"


def retire_phase_ledger(ticket: "Ticket") -> None:
    """Retire every session's phase ledger for *ticket* (#1286).

    Mirrors ``lifecycle clear-ledger --confirm``: a per-session reset of
    ``visited_phases`` and ``phase_visits`` so
    the next workstream re-earns its attestations. Wrapped in
    ``transaction.atomic`` so ``select_for_update`` works even when the FSM caller
    (the loop ``reopen_ticket`` mechanical path) has no surrounding transaction.
    """
    with transaction.atomic():
        for session in ticket.sessions.select_for_update().all():  # type: ignore[attr-defined]  # Django reverse FK
            session.visited_phases = []
            session.phase_visits = {}
            session.save(update_fields=["visited_phases", "phase_visits"])


def stamp_reopened_over(ticket: "Ticket") -> None:
    """Record the merged PRs a reopen covers; a PR row carries no merge time to compare (#5031)."""
    urls = list(_merged_rows(ticket).values_list("url", flat=True))
    ticket.extra = {**(ticket.extra or {}), _REOPENED_OVER: urls}


def all_merges_reopened_over(ticket: "Ticket") -> bool:
    reopened_over = (ticket.extra or {}).get(_REOPENED_OVER)
    return bool(reopened_over) and not _merged_rows(ticket).exclude(url__in=reopened_over).exists()


def _merged_rows(ticket: "Ticket") -> "QuerySet[PullRequest]":
    return PullRequest.objects.filter(ticket=ticket, state=PullRequest.State.MERGED)
