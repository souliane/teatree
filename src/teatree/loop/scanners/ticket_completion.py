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

from teatree.backends.loader import get_code_host_for_url
from teatree.core.overlay import OverlayBase
from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.models import Ticket

logger = logging.getLogger(__name__)


def _has_draft_mrs(ticket: "Ticket") -> bool:
    """Return True if any MR in the ticket's extra is still a draft."""
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    mrs = extra.get("mrs", {})
    if not isinstance(mrs, dict):
        return False
    return any(isinstance(mr, dict) and mr.get("draft") for mr in mrs.values())


_COMPLETABLE_STATES: frozenset[str] = frozenset({"shipped", "in_review", "merged"})


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
                if _has_draft_mrs(ticket):
                    signals.append(
                        ScanSignal(
                            kind="ticket.reopen_needed",
                            summary=f"Ticket {ticket.ticket_number} — draft MRs exist, reopening",
                            payload={
                                "ticket_id": ticket.pk,
                                "ticket_state": ticket.state,
                                "issue_url": ticket.issue_url,
                            },
                        ),
                    )
                    continue

                host = get_code_host_for_url(self.overlay, ticket.issue_url)
                if host is None:
                    continue
                try:
                    issue_data = host.get_issue(ticket.issue_url)
                except Exception:  # noqa: BLE001 — an issue-fetch failure skips the ticket, never aborts the scan
                    logger.warning("Failed to fetch issue for ticket %s (%s), skipping", ticket.pk, ticket.issue_url)
                    continue
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
            except Exception:
                logger.exception("TicketCompletionScanner failed on ticket %s", ticket.pk)
                continue
        return signals

    def _candidate_tickets(self) -> Iterable["Ticket"]:
        ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))
        qs = ticket_model.objects.filter(state__in=_COMPLETABLE_STATES).exclude(issue_url="")
        if self.overlay_name:
            qs = qs.filter(overlay=self.overlay_name)
        return qs.only("id", "issue_url", "state", "overlay")
