"""Pane-reaper scanner — demote idle maker panes when teams is enabled (#1838 PR#7b).

The consumer-side wiring PR#7a deferred (to avoid the inertness gate). A global
(``overlay=""``) mechanical scanner that, ONLY when ``teams_enabled``, demotes
idle maker panes — a ``team:<role>`` claim with no live Session past
``teams_idle_minutes`` — to STOPPED via
:func:`teatree.teams.pane_reaper.reap_idle_panes`, freeing the slot for a future
spawn. It emits one ``team_pane.reaped`` signal per demotion (informational —
the demotion already happened; releasing your own stale claim is a mechanical,
reversible action, the same risk class as ``reclaim_orphaned_claims``).

DEFAULT-OFF: when ``teams_enabled`` is false the scanner returns ``[]`` WITHOUT
touching the pane reaper — the mini-loop's ``build_jobs`` already returns no job
when teams is off, and this guard is the in-scanner belt-and-braces so an
explicitly-constructed disabled scanner is provably a no-op.

Fail-safe (mirrors the underlying reaper): every uncertainty in
:func:`teatree.teams.pane_reaper.reapable_panes` resolves to KEEP, so only a
confirmed-stale, no-live-session pane is reaped.
"""

import logging
from dataclasses import dataclass

from teatree.loop.scanners.base import ScanSignal
from teatree.teams.pane_reaper import reap_idle_panes, reapable_panes

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PaneReaperScanner:
    """Demote idle maker panes to STOPPED when ``teams_enabled`` — one signal per reap."""

    teams_enabled: bool
    idle_minutes: int = 30
    name: str = "pane_reaper"

    def scan(self) -> list[ScanSignal]:
        if not self.teams_enabled:
            return []
        try:
            candidates = list(reapable_panes(idle_minutes=self.idle_minutes))
        except Exception:
            logger.exception("pane_reaper: reapable_panes failed — skipping tick")
            return []
        if not candidates:
            return []
        try:
            reaped = reap_idle_panes(idle_minutes=self.idle_minutes)
        except Exception:
            logger.exception("pane_reaper: reap_idle_panes failed — skipping tick")
            return []
        # Emit one signal per ACTUAL demotion: the candidate snapshot may exceed
        # the reaped count if a concurrent heartbeat/stop landed between the scan
        # and the conditional UPDATE (the reaper re-asserts the predicate), so
        # never claim more reaps than the reaper confirmed.
        return [
            ScanSignal(
                kind="team_pane.reaped",
                summary=f"idle maker pane {task.claimed_by} on ticket {task.ticket.pk} — demoted to stopped",
                payload={
                    "task_id": task.pk,
                    "ticket_id": task.ticket.pk,
                    "claim_slot": task.claimed_by,
                },
            )
            for task in candidates[:reaped]
        ]


__all__ = ["PaneReaperScanner"]
