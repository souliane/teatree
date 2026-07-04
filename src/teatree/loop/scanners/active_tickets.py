"""Surface FSM state for tickets in non-terminal states.

Emits one ``ticket.active`` signal per ticket with a state between
``not_started`` and ``retrospected`` (inclusive), excluding ``delivered``
and ``ignored``.  The statusline uses these to show at-a-glance
lifecycle progress grouped by overlay.

#1156: the signal's ``title`` prefers ``ticket.short_description`` (the
AI-generated terminal-friendly summary) over the cached tracker title
in ``extra["issue_title"]``. Blank ``short_description`` falls back to
the cached title — no behaviour change for un-described tickets. A
ticket whose source tracker reports 404 (``extra["tracker_404"]``)
emits ``issue_url=""`` so the renderer drops the clickable wrap and
prints a bare ``#N`` instead of an unreachable link.
``short_description`` is generated lazily: when a ticket has a
non-blank ``extra["issue_title"]`` but blank ``short_description``,
the scanner enqueues a ``Task(phase="short_describe",
execution_target=HEADLESS)``. The actual LLM call lives in the
headless worker — no synchronous LLM in scan().
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from django.apps import apps

from teatree.loop.scanners.base import ScanSignal

logger = logging.getLogger(__name__)

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
        for ticket in qs.only("id", "state", "overlay", "issue_url", "extra", "short_description", "expedited"):
            try:
                extra = ticket.extra if isinstance(ticket.extra, dict) else {}
                cached_title = extra.get("issue_title", "") if isinstance(extra, dict) else ""
                cached_title = cached_title if isinstance(cached_title, str) else ""
                title = ticket.short_description or cached_title
                # ``tracker_404`` is the last-observed-404 marker the tracker
                # client persists on the ticket extras; the renderer drops the
                # URL when set so dead permalinks don't surface (#1163, #1156).
                tracker_404 = bool(extra.get("tracker_404", False)) if isinstance(extra, dict) else False
                issue_url = "" if tracker_404 else ticket.issue_url
                if not ticket.short_description and cached_title:
                    _enqueue_short_describe(ticket)
                signals.append(
                    ScanSignal(
                        kind="ticket.active",
                        summary=f"#{ticket.ticket_number} {ticket.state}",
                        payload={
                            "ticket_id": ticket.pk,
                            "ticket_number": ticket.ticket_number,
                            "state": ticket.state,
                            "issue_url": issue_url,
                            "title": title,
                            "tracker_404": tracker_404,
                            "expedite": ticket.expedited,
                        },
                    ),
                )
            except Exception:
                logger.exception("ActiveTicketsScanner failed on ticket %s", ticket.pk)
                continue
        return signals


def _enqueue_short_describe(ticket: "Ticket") -> None:
    """Idempotently enqueue a headless ``short_describe`` task for *ticket*.

    Skips when a non-terminal task with the same phase already exists for
    the ticket — at-least-once delivery from django-tasks means the loop
    may scan again before the previous task lands.
    """
    from teatree.core.models import Task  # noqa: PLC0415
    from teatree.core.models.session import Session  # noqa: PLC0415

    existing = Task.objects.filter(
        ticket=ticket,
        phase="short_describe",
        status__in=[Task.Status.PENDING, Task.Status.CLAIMED, Task.Status.COMPLETED],
    )
    if existing.exists():
        return
    session = Session.objects.create(ticket=ticket, agent_id="short-describe")
    Task.objects.create(
        ticket=ticket,
        session=session,
        phase="short_describe",
        execution_target=Task.ExecutionTarget.HEADLESS,
        subject=f"Summarize #{ticket.ticket_number}",
        execution_reason="Auto-scheduled short_describe — generate terminal-friendly ticket summary",
    )
