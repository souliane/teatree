"""Detect tickets whose upstream issue indicates all work is complete.

Walks tickets in post-ship states (``shipped``, ``in_review``, ``merged``)
and checks whether the upstream issue/ticket is done via the overlay's
``is_issue_done()`` hook.  This covers the gap where ``MyPrsScanner``
only sees open PRs and ``TicketDispositionScanner`` only covers pre-PR
states — leaving post-ship tickets stuck forever when the issue is
closed externally (e.g. auto-closed by a merge, or label-advanced by CI).

Emits ``ticket.completion_detected`` signals for mechanical dispatch:
the dispatcher transitions the ticket through the remaining FSM states
(``mark_merged`` → ``retrospect`` → ``mark_delivered``) without agent
involvement.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from django.apps import apps

from teatree.backends.loader import issue_is_done
from teatree.core.overlay import OverlayBase
from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.models import Ticket

logger = logging.getLogger(__name__)


def _has_draft_prs(ticket: "Ticket") -> bool:
    """Return True if any PR in the ticket's extra is still a draft."""
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    prs = extra.get("prs", {})
    if not isinstance(prs, dict):
        return False
    return any(isinstance(pr, dict) and pr.get("draft") for pr in prs.values())


@dataclass(slots=True)
class TicketCompletionScanner:
    """Yield ``ticket.completion_detected`` for post-ship tickets whose issue is done."""

    overlay: OverlayBase
    overlay_name: str = ""
    name: str = "ticket_completion"

    def scan(self) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        for ticket in self._candidate_tickets():
            try:
                if _has_draft_prs(ticket):
                    signals.append(
                        ScanSignal(
                            kind="ticket.reopen_needed",
                            summary=f"Ticket {ticket.ticket_number} — draft PRs exist, reopening",
                            payload={
                                "ticket_id": ticket.pk,
                                "ticket_state": ticket.state,
                                "issue_url": ticket.issue_url,
                            },
                        ),
                    )
                    continue

                if issue_is_done(self.overlay, ticket.issue_url):
                    signals.append(
                        ScanSignal(
                            kind="ticket.completion_detected",
                            summary=f"Ticket {ticket.ticket_number} — issue done upstream",
                            payload={
                                "ticket_id": ticket.pk,
                                "ticket_state": ticket.state,
                                "issue_url": ticket.issue_url,
                            },
                        ),
                    )
            except Exception:
                logger.exception("TicketCompletionScanner failed on ticket %s", ticket.pk)
                continue
        return signals

    def _candidate_tickets(self) -> Iterable["Ticket"]:
        ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))
        qs = ticket_model.objects.filter(state__in=ticket_model.completable_states()).exclude(issue_url="")
        if self.overlay_name:
            qs = qs.filter(overlay=self.overlay_name)
        return qs.only("id", "issue_url", "state", "overlay")
