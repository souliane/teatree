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
        cadence = PhaseCadence(self.overlay_name, phase=SCANNING_NEWS_PHASE, cadence_hours=self.cadence_hours)
        if cadence.in_flight_exists():
            return []

        trigger = cadence.evaluate_trigger(now=timezone.now(), last_run_at=cadence.last_run_at())
        if trigger is None:
            return []

        task = cadence.queue_task(
            placeholder_issue_url=f"scanning-news://{self.overlay_name}",
            agent_id=f"scanning-news-{self.overlay_name}",
            execution_reason=self._execution_reason(trigger),
            subject=f"Scan AI news: {self.overlay_name}",
            log_label="ScanningNewsScanner",
        )
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

    def _execution_reason(self, trigger: str) -> str:
        """Build the dispatcher directive, embedding the ask-gate contract (#1391).

        When ``require_approval`` is on (the default), the directive
        carries an explicit instruction that the skill must record each
        candidate as a ``PendingArticleSuggestion`` and surface the batch
        for user approval — it must NOT auto-create issues. The marker
        substring is load-bearing: it is the channel the dispatched skill
        reads to know the gate is active.

        The MERGED source table (#3669) is appended to every directive, gate or
        not: the agent runs shell-denied and fetches by URL, so the breadth the
        press-review aggregator contributed has to reach it as text. The
        teatree-relevance triage the loop exists for is unchanged — the directive
        names the sources, the skill still decides what survives.
        """
        base = f"Periodic scanning-news scan ({trigger}) via skill: {self.skill}"
        if self.require_approval:
            base = (
                f"{base} | ASK-GATE: do NOT auto-create issues — record each candidate as a "
                f"PendingArticleSuggestion and surface the batch for explicit user approval (#1391)"
            )
        return f"{base}\n{render_source_directive(NEWS_SOURCES)}"


__all__ = [
    "SCANNING_NEWS_PHASE",
    "ScanningNewsScanner",
]
