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

That enqueue dedups on the ARTIFACT, not on a task: only in-flight work
suppresses a duplicate, because a terminal task proves an attempt was made
and never that the field was written. The per-ticket attempt budget
(``Ticket.consume_phase_attempt``) is what keeps dedup-by-artifact bounded.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from django.apps import apps

from teatree.core.modelkit.phases import SHORT_DESCRIBE_PHASE
from teatree.loop.scanners.base import ScanSignal

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from teatree.core.models import Ticket

_TERMINAL_STATES: frozenset[str] = frozenset({"delivered", "review_posted", "ignored"})

#: How many ``short_describe`` tasks this scanner may enqueue for one ticket before
#: giving up on it permanently. Three covers a transient failure of the one-shot turn
#: (timeout, backend blip); past that the write path is broken rather than unlucky,
#: because the deterministic runner is a single pass that falls back to truncating the
#: cached title, so a run that neither writes nor raises will not write on a fourth try
#: either. Giving up is safe rather than lossy — ``title`` below already falls back to
#: the cached tracker title, so a spent ticket renders exactly as it did before the
#: summariser existed. Mirrors ``IncomingEvent``'s ``MAX_INGEST_ATTEMPTS``: a module
#: constant owned by the caller, not a config setting.
SHORT_DESCRIBE_MAX_ATTEMPTS: Final[int] = 3


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

    Dedups on IN-FLIGHT work only (``Task.Status.active()``), the same filter
    every sibling scanner uses: at-least-once delivery from django-tasks means
    the loop may scan again before the previous task lands, so a PENDING or
    CLAIMED task is a real duplicate.

    A TERMINAL task is deliberately NOT a duplicate. The caller reaches here
    only when ``short_description`` is blank — it has just observed that the
    artifact this phase owed is absent — so treating a COMPLETED task as "handled"
    would contradict the very condition that got us here. A task is evidence that
    an attempt happened, never that it delivered; only the field itself is
    evidence of the field. Counting a completion as done is what let an agent that
    called ``task_complete`` without writing anything wedge a ticket blank
    permanently, since the dedup then matched forever and no tick could re-enqueue.

    ``consume_phase_attempt`` is the other half: dedup-by-artifact re-enqueues for
    as long as the artifact is missing, so a phase that can never write its field
    would spin every tick. The budget bounds that and gives up terminally; a spent
    ticket keeps rendering its cached tracker title.
    """
    from teatree.core.models import Task  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.core.models.session import Session  # noqa: PLC0415 — deferred: ORM import needs the app registry

    in_flight = Task.objects.filter(
        ticket=ticket,
        phase=SHORT_DESCRIBE_PHASE,
        status__in=Task.Status.active(),
    )
    if in_flight.exists():
        return
    if not ticket.consume_phase_attempt(SHORT_DESCRIBE_PHASE, max_attempts=SHORT_DESCRIBE_MAX_ATTEMPTS):
        return
    session = Session.objects.create(ticket=ticket, agent_id="short-describe")
    Task.objects.create(
        ticket=ticket,
        session=session,
        phase=SHORT_DESCRIBE_PHASE,
        execution_target=Task.ExecutionTarget.HEADLESS,
        subject=f"Summarize #{ticket.ticket_number}",
        execution_reason="Auto-scheduled short_describe — generate terminal-friendly ticket summary",
    )
