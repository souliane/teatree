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

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from django.apps import apps

from teatree.backends.protocols import CodeHostBackend
from teatree.core.overlay import OverlayBase
from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.models import Ticket

_COMPLETABLE_STATES: frozenset[str] = frozenset({"shipped", "in_review", "merged"})


@dataclass(slots=True)
class TicketCompletionScanner:
    """Yield ``ticket.completion_detected`` for post-ship tickets whose issue is done."""

    host: CodeHostBackend
    overlay: OverlayBase
    overlay_name: str = ""
    name: str = "ticket_completion"

    def scan(self) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        for ticket in self._candidate_tickets():
            issue_data = self.host.get_issue(ticket.issue_url)
            if not isinstance(issue_data, dict) or "error" in issue_data:
                continue
            if self.overlay.is_issue_done(issue_data):
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
        return signals

    def _candidate_tickets(self) -> Iterable["Ticket"]:
        ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))
        qs = ticket_model.objects.filter(state__in=_COMPLETABLE_STATES).exclude(issue_url="")
        if self.overlay_name:
            qs = qs.filter(overlay=self.overlay_name)
        return qs.only("id", "issue_url", "state", "overlay")
