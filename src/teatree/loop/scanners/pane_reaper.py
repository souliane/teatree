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

Display teardown (WI-5, #1838). When the PRESENTATION layer is active
(``display_enabled``), a demotion is the trigger to kill the demoted pane's tmux
split. The teardown is DB-DRIVEN: after the reaper releases the stale claims, the
scanner reconciles tmux against the LIVE team claims
(:func:`teatree.teams.pane_display.reconcile_orphan_panes`) — every ``team:*``-
titled pane whose slot is no longer a live claim is killed, so the demoted pane's
split closes while a still-live teammate's split is untouched. The DB lease stays
authoritative; tmux is a downstream effect reconciled to it. Best-effort: the
reconcile never raises into the tick (no tmux / no panes is a no-op), and it is
skipped entirely when display is off (byte-identical to the pre-WI-5 reaper).

Fail-safe (mirrors the underlying reaper): every uncertainty in
:func:`teatree.teams.pane_reaper.reapable_panes` resolves to KEEP, so only a
confirmed-stale, no-live-session pane is reaped.
"""

import logging
from dataclasses import dataclass

from django.utils import timezone

from teatree.core.models.task import Task
from teatree.loop.scanners.base import ScanSignal
from teatree.teams.pane_display import reconcile_orphan_panes
from teatree.teams.pane_reaper import reap_idle_panes, reapable_panes
from teatree.teams.roles import TEAM_CLAIM_PREFIX

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PaneReaperScanner:
    """Demote idle maker panes to STOPPED when ``teams_enabled`` — one signal per reap."""

    teams_enabled: bool
    idle_minutes: int = 30
    display_enabled: bool = False
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
        demoted = candidates[:reaped]
        if demoted:
            self._teardown_display_panes()
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
            for task in demoted
        ]

    def _teardown_display_panes(self) -> None:
        """Reconcile tmux panes against the live team claims after a demotion (WI-5).

        Skipped when the presentation layer is off (byte-identical to the
        pre-WI-5 reaper). Best-effort: a tmux failure never raises into the tick.
        """
        if not self.display_enabled:
            return
        try:
            reconcile_orphan_panes(live_claim_slots=_live_team_claim_slots())
        except Exception:
            logger.exception("pane_reaper: tmux pane reconcile failed — leaving panes")


def _live_team_claim_slots() -> set[str]:
    """The ``team:<role>`` slots still held by a live (CLAIMED, unexpired) claim."""
    rows = Task.objects.filter(
        status=Task.Status.CLAIMED,
        claimed_by__startswith=TEAM_CLAIM_PREFIX,
        lease_expires_at__gt=timezone.now(),
    ).values_list("claimed_by", flat=True)
    return set(rows)


__all__ = ["PaneReaperScanner"]
