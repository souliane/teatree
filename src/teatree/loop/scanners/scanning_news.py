"""Periodic scanning-news scanner — #1191, #1267.

Companion to the ``scanning-news`` skill (#1190): the loop should fire a
daily ``scanning_news`` task that runs the news-scan / improvement-ideas
workflow without depending on an external cron. The scanner is one of the
periodic task-queuing family that share
:class:`teatree.loop.scanners.phase_cadence.PhaseCadence`, deliberately simple:

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
    recent task is the "last run" timestamp.
"""

from dataclasses import dataclass

from django.utils import timezone

from teatree.core.news_sources import NEWS_SOURCES, render_source_directive
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.phase_cadence import PhaseCadence

#: Canonical phase token written to ``Task.phase`` for scanning-news tasks.
SCANNING_NEWS_PHASE = "scanning_news"


@dataclass(slots=True)
class ScanningNewsScanner:
    """Queue a periodic ``scanning_news`` task for the active core overlay.

    Configuration fields are passed explicitly (rather than read from a
    global at scan time) so test setup is deterministic and the wiring
    layer is the single place that resolves
    :class:`teatree.config.UserSettings` and
    :func:`teatree.config.discover_active_overlay` to scanner kwargs. The
    on/off decision is the ``news`` ``Loop`` row and the active preset; the
    scanner itself always scans when invoked.

    ``overlay_name`` is the resolved overlay-anchor identity for the
    placeholder ticket (#1267). The scanner never reads or assumes the
    name — it stamps whatever value the wiring layer hands it. The
    canonical post-0027 default in production is ``"t3-teatree"``.

    The queued task directs the shell-denied agent to decide each candidate;
    the server records and acts on those decisions through the issue facade.
    """

    overlay_name: str
    skill: str = "scanning-news"
    cadence_hours: int = 24
    name: str = "scanning_news"

    def scan(self) -> list[ScanSignal]:
        cadence = PhaseCadence(self.overlay_name, phase=SCANNING_NEWS_PHASE, cadence_hours=self.cadence_hours)
        trigger, signals = cadence.gate(
            now=timezone.now(), held_kind="scanning_news.held", not_due_kind="scanning_news.not_due"
        )
        if trigger is None:
            return signals

        task = cadence.queue_task(
            placeholder_issue_url=f"scanning-news://{self.overlay_name}",
            agent_id=f"scanning-news-{self.overlay_name}",
            execution_reason=self._execution_reason(trigger),
            subject=f"Scan AI news: {self.overlay_name}",
            log_label="ScanningNewsScanner",
        )
        return cadence.handover(
            task,
            ScanSignal(
                kind="scanning_news.queued",
                summary=f"scanning-news queued for {self.overlay_name} (trigger: {trigger})",
                payload={
                    "overlay": self.overlay_name,
                    "skill": self.skill,
                    "phase": SCANNING_NEWS_PHASE,
                    "task_id": task.pk if task is not None else None,
                    "trigger": trigger,
                },
            ),
            held_kind="scanning_news.held",
        )

    def _execution_reason(self, trigger: str) -> str:
        """Build the dispatcher directive with the article decision contract."""
        base = f"Periodic scanning-news scan ({trigger}) via skill: {self.skill}"
        base = (
            f"{base} | decide every candidate by backlog fit, a clean base, and open-ticket duplicates; "
            "return article_suggestions so the server settles each row and files only fitting ideas"
        )
        return f"{base}\n{render_source_directive(NEWS_SOURCES)}"


__all__ = [
    "SCANNING_NEWS_PHASE",
    "ScanningNewsScanner",
]
