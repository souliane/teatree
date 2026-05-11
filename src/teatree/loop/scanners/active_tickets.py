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
        return [
            ScanSignal(
                kind="ticket.active",
                summary=f"#{ticket.ticket_number} {ticket.state}",
                payload={
                    "ticket_id": ticket.pk,
                    "ticket_number": ticket.ticket_number,
                    "state": ticket.state,
                },
            )
            for ticket in qs.only("id", "state", "overlay", "issue_url")
        ]
