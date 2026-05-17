"""Flag tickets with no recent activity so they surface in the statusline.

Staleness is derived entirely from data the loop already records — no new
model fields, no migrations, no external API calls (issue #563). The
primary activity signal is :class:`TaskAttempt.started_at` (the last
agent run); when a ticket has no recorded attempts the fallback is
:class:`TicketTransition.created_at` (the last phase change).

Staleness is measured on *activity*, not phase duration: a ticket worked
on every day stays fresh even after a week in ``coding``. Tickets in
``not_started`` (no work expected yet) or a terminal state (``shipped``,
``merged``, ``retrospected``, ``delivered``, ``ignored`` — nothing left
to do) are excluded.

This scanner only **reports**. It never transitions the :class:`Ticket`.
The dispatcher routes ``ticket.stale`` into the statusline
``action_needed`` zone (BLUEPRINT §5.6.1) so the operator decides.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db.models import Max
from django.utils import timezone

from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)

# States in which a staleness warning is meaningful: work is expected but
# the ticket has not yet reached a terminal state. ``not_started`` is
# excluded (no work scheduled yet); the terminal set has nothing left to do.
_STALE_CANDIDATE_STATES: frozenset[str] = frozenset(
    {"scoped", "started", "coded", "tested", "reviewed", "in_review"},
)

DEFAULT_STALE_THRESHOLD_DAYS = 3


@dataclass(slots=True)
class StaleTicketsScanner:
    """Emit ``ticket.stale`` for tickets idle longer than *threshold_days*.

    Read-only: iterates the local :class:`Ticket` table (filtered by
    *overlay_name* when set) and aggregates the newest
    :class:`TaskAttempt`/:class:`TicketTransition` timestamp per ticket. No
    writes, no network.
    """

    overlay_name: str = ""
    threshold_days: int = DEFAULT_STALE_THRESHOLD_DAYS
    name: str = "stale_tickets"

    def scan(self) -> list[ScanSignal]:
        now = timezone.now()
        signals: list[ScanSignal] = []
        for ticket in self._candidate_tickets():
            last_activity = self._last_activity(ticket)
            if last_activity is None:
                continue
            age_days = (now - last_activity).days
            if age_days < self.threshold_days:
                continue
            signals.append(
                ScanSignal(
                    kind="ticket.stale",
                    summary=f"TICKET-{ticket.ticket_number} stale in {ticket.state} ({age_days}d)",
                    payload={
                        "ticket_id": ticket.pk,
                        "ticket_number": ticket.ticket_number,
                        "ticket_state": ticket.state,
                        "age_days": age_days,
                    },
                ),
            )
        return signals

    def _candidate_tickets(self) -> Iterable["Ticket"]:
        ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))
        qs = ticket_model.objects.filter(state__in=_STALE_CANDIDATE_STATES)
        if self.overlay_name:
            qs = qs.filter(overlay=self.overlay_name)
        return qs.only("id", "state", "issue_url", "overlay")

    @staticmethod
    def _last_activity(ticket: "Ticket") -> datetime | None:
        last_attempt = ticket.tasks.aggregate(  # ty: ignore[unresolved-attribute]
            ts=Max("attempts__started_at"),
        )["ts"]
        if last_attempt is not None:
            return last_attempt
        return ticket.transitions.aggregate(  # ty: ignore[unresolved-attribute]
            ts=Max("created_at"),
        )["ts"]
