"""Surface FSM state for tickets in non-terminal states.

Emits one ``ticket.active`` signal per ticket with a state between
``not_started`` and ``retrospected`` (inclusive), excluding ``delivered``
and ``ignored``.  The statusline uses these to show at-a-glance
lifecycle progress grouped by overlay.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from django.apps import apps

from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.models import Ticket

_TERMINAL_STATES: frozenset[str] = frozenset({"delivered", "ignored"})


@dataclass(slots=True)
class ActiveTicketsScanner:
    overlay_name: str = ""
    name: str = "active_tickets"

    def scan(self) -> list[ScanSignal]:
        ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))
        qs = ticket_model.objects.exclude(state__in=_TERMINAL_STATES).order_by("id")
        if self.overlay_name:
            qs = qs.filter(overlay=self.overlay_name)
        signals: list[ScanSignal] = []
        # ``extra["issue_title"]`` is the cached human title from the tracker
        # (see ``TicketExtra``). Surfacing it on the active signal lets the
        # statusline render the canonical ``#N (short desc)`` item shape
        # without re-fetching from the tracker on every tick (#1015).
        for ticket in qs.only("id", "state", "overlay", "issue_url", "extra"):
            extra = ticket.extra if isinstance(ticket.extra, dict) else {}
            title = extra.get("issue_title", "") if isinstance(extra, dict) else ""
            signals.append(
                ScanSignal(
                    kind="ticket.active",
                    summary=f"#{ticket.ticket_number} {ticket.state}",
                    payload={
                        "ticket_id": ticket.pk,
                        "ticket_number": ticket.ticket_number,
                        "state": ticket.state,
                        "issue_url": ticket.issue_url,
                        "title": title if isinstance(title, str) else "",
                    },
                ),
            )
        return signals
