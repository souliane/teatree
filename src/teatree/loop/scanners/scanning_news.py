"""Periodic scanning-news scanner — #1191, #1267.

Companion to the ``scanning-news`` skill (#1190): the loop should fire a
daily ``scanning_news`` task that runs the news-scan / improvement-ideas
workflow without depending on an external cron. The scanner mirrors the
shape of :mod:`teatree.loop.scanners.architectural_review` but is
deliberately simpler:

* **Single trigger.** Only a cadence (``scanning_news_cadence_hours``,
    default 24h). There is no after-merge backstop — news scanning is a
    fixed-rate platform behaviour, not coupled to delivery velocity.
* **Overlay anchor is injected, not baked.** This is a core scanner —
    it does not know any overlay's name. The wiring layer
    (``teatree.loop.global_scanner_factories._scanning_news_scanner``) resolves the
    active core overlay via :func:`teatree.config.discover_active_overlay`
    and passes the result as the ``overlay_name`` constructor kwarg.
    Tasks queued by the scanner are anchored at a placeholder Ticket
    keyed off that resolved name (#1267 — pre-fix this module hardcoded
    the legacy ``"teatree"`` value, which migration 0027 had already
    canonicalized to ``"t3-teatree"``).
* **Same dedup contract.** A pending or claimed ``scanning_news`` task
    acts as the lock — completion (or failure) unlocks the next cadence
    window. No new model fields; the ``Session.started_at`` of the most
    recent task is the "last run" timestamp (same trick as
    ``architectural_review``).
"""

import datetime as dt
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from teatree.loop.scanners.base import ScanSignal, hours_since

if TYPE_CHECKING:
    from teatree.core.models import Session as _Session
    from teatree.core.models import Task as _Task
    from teatree.core.models import Ticket as _Ticket

logger = logging.getLogger(__name__)

#: Canonical phase token written to ``Task.phase`` for scanning-news tasks.
SCANNING_NEWS_PHASE = "scanning_news"

#: States that mean a news task is still in-flight (cannot queue a dupe).
_IN_FLIGHT_TASK_STATES: frozenset[str] = frozenset({"pending", "claimed"})


@dataclass(slots=True)
class ScanningNewsScanner:
    """Queue a periodic ``scanning_news`` task for the active core overlay.

    Configuration fields are passed explicitly (rather than read from a
    global at scan time) so test setup is deterministic and the wiring
    layer is the single place that resolves
    :class:`teatree.config.UserSettings` and
    :func:`teatree.config.discover_active_overlay` to scanner kwargs. The
    on/off decision lives at the wiring layer
    (``scanning_news_disabled`` in core config); the scanner itself
    always scans when invoked.

    ``overlay_name`` is the resolved overlay-anchor identity for the
    placeholder ticket (#1267). The scanner never reads or assumes the
    name — it stamps whatever value the wiring layer hands it. The
    canonical post-0027 default in production is ``"t3-teatree"``.

    ``require_approval`` (#1391) is the ask-gate flag, resolved from
    ``ask_before_creating_news_tickets`` at the wiring layer. When true
    (the default), the queued task's directive instructs the dispatched
    skill to record each candidate article as a
    :class:`teatree.core.models.pending_article_suggestion.PendingArticleSuggestion`
    and surface the batch for explicit user approval — it must NOT
    auto-create issues. The scanner never creates issues itself; this
    flag is the contract it stamps onto the task so the skill cannot
    silently fall back to mass-filing.
    """

    overlay_name: str
    skill: str = "scanning-news"
    cadence_hours: int = 24
    require_approval: bool = True
    name: str = "scanning_news"

    def scan(self) -> list[ScanSignal]:
        if self._in_flight_task_exists():
            return []

        now = timezone.now()
        last_run_at = self._last_run_at()
        trigger = self._evaluate_trigger(now=now, last_run_at=last_run_at)
        if trigger is None:
            return []

        task = self._queue_task(trigger=trigger)
        if task is None:
            return []
        return [
            ScanSignal(
                kind="scanning_news.queued",
                summary=f"scanning-news queued for {self.overlay_name} (trigger: {trigger})",
                payload={
                    "overlay": self.overlay_name,
                    "skill": self.skill,
                    "phase": SCANNING_NEWS_PHASE,
                    "task_id": task.pk,
                    "trigger": trigger,
                    "require_approval": self.require_approval,
                },
            ),
        ]

    def _in_flight_task_exists(self) -> bool:
        """True iff a pending/claimed scanning-news task already exists."""
        task_model = _task_model()
        if task_model is None:
            return False
        return task_model.objects.filter(
            ticket__overlay=self.overlay_name,
            phase=SCANNING_NEWS_PHASE,
            status__in=_IN_FLIGHT_TASK_STATES,
        ).exists()

    def _last_run_at(self) -> dt.datetime | None:
        """Return the most recent task's Session.started_at, or None.

        ``None`` when no prior scanning-news task has been recorded — the
        bootstrap case where the cadence is trivially elapsed.
        """
        task_model = _task_model()
        if task_model is None:
            return None
        aggregate = task_model.objects.filter(
            ticket__overlay=self.overlay_name,
            phase=SCANNING_NEWS_PHASE,
        ).aggregate(ts=Max("session__started_at"))
        return aggregate["ts"]

    def _evaluate_trigger(self, *, now: dt.datetime, last_run_at: dt.datetime | None) -> str | None:
        """Return the trigger name (``bootstrap`` / ``cadence``) or None."""
        if last_run_at is None:
            return "bootstrap"
        if hours_since(last_run_at, now=now) >= self.cadence_hours:
            return "cadence"
        return None

    def _queue_task(self, *, trigger: str) -> "_Task | None":
        """Create a Task + Session row anchored at the overlay placeholder ticket.

        Wrapped in ``transaction.atomic()`` so a concurrent scanner on a
        second loop process can't double-queue: the in-flight check and
        the insert run under one DB transaction. A DB error is logged
        but never raised — losing one tick's news queue is acceptable;
        crashing the tick is not.
        """
        ticket_model = _ticket_model()
        task_model = _task_model()
        session_model = _session_model()
        if ticket_model is None or task_model is None or session_model is None:
            return None
        try:
            with transaction.atomic():
                ticket, _created = ticket_model.objects.get_or_create(
                    issue_url=self._placeholder_issue_url(),
                    defaults={"overlay": self.overlay_name, "role": "author"},
                )
                # Keep the overlay tag in sync even if the placeholder
                # ticket pre-dates the current wiring (e.g. the legacy
                # ``"teatree"`` row left over before #1267 / migration 0027).
                if ticket.overlay != self.overlay_name:
                    ticket.overlay = self.overlay_name
                    ticket.save(update_fields=["overlay"])
                session = session_model.objects.create(
                    overlay=self.overlay_name,
                    ticket=ticket,
                    agent_id=f"scanning-news-{self.overlay_name}",
                )
                return task_model.objects.create(
                    ticket=ticket,
                    session=session,
                    phase=SCANNING_NEWS_PHASE,
                    execution_target=task_model.ExecutionTarget.HEADLESS,
                    subject=f"Scan AI news: {self.overlay_name}",
                    execution_reason=self._execution_reason(trigger),
                )
        except Exception:
            logger.exception("ScanningNewsScanner: failed to queue scanning-news task")
            return None

    def _execution_reason(self, trigger: str) -> str:
        """Build the dispatcher directive, embedding the ask-gate contract (#1391).

        When ``require_approval`` is on (the default), the directive
        carries an explicit instruction that the skill must record each
        candidate as a ``PendingArticleSuggestion`` and surface the batch
        for user approval — it must NOT auto-create issues. The marker
        substring is load-bearing: it is the channel the dispatched skill
        reads to know the gate is active.
        """
        base = f"Periodic scanning-news scan ({trigger}) via skill: {self.skill}"
        if self.require_approval:
            return (
                f"{base} | ASK-GATE: do NOT auto-create issues — record each candidate as a "
                f"PendingArticleSuggestion and surface the batch for explicit user approval (#1391)"
            )
        return base

    def _placeholder_issue_url(self) -> str:
        """Stable synthetic URL for the overlay-anchored placeholder ticket."""
        return f"scanning-news://{self.overlay_name}"


def _ticket_model() -> "type[_Ticket] | None":
    try:
        return cast("type[_Ticket]", apps.get_model("core", "Ticket"))
    except Exception:  # noqa: BLE001 — a probe failure must never break the tick; degrade to no signal
        return None


def _task_model() -> "type[_Task] | None":
    try:
        return cast("type[_Task]", apps.get_model("core", "Task"))
    except Exception:  # noqa: BLE001 — a probe failure must never break the tick; degrade to no signal
        return None


def _session_model() -> "type[_Session] | None":
    try:
        return cast("type[_Session]", apps.get_model("core", "Session"))
    except Exception:  # noqa: BLE001 — a probe failure must never break the tick; degrade to no signal
        return None


__all__ = [
    "SCANNING_NEWS_PHASE",
    "ScanningNewsScanner",
]
