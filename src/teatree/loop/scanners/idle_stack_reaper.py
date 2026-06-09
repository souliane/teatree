"""Idle-stack reaper scanner — frees an idle stack's slot + RAM (souliane/teatree#2190).

A global (``overlay=""``) mechanical scanner. Each cadence window it asks
:func:`teatree.core.gates.idle_stack.reapable_worktrees` for the idle running
worktrees of its overlay and emits one ``local_stack.reap_idle`` signal per
candidate. The paired mechanical handler (``reap_idle_stack``) re-verifies the
live state and fires ``Worktree.stop_services`` (REVERSIBLE — DB + worktree
preserved). The maker/checker boundary is irrelevant here: the scanner only
flags candidates, it never stops anything itself.

Cadence-gated by :class:`LocalStackReaperMarker` so a sub-minute tick does not
re-scan + re-shell ``docker ps`` every tick (mirrors
:class:`ResourcePressureScanner`). Best-effort: a marker/DB error logs and
returns an empty signal list rather than crashing the tick.
"""

import logging
from dataclasses import dataclass

from django.utils import timezone

from teatree.core.gates.idle_stack import reapable_worktrees
from teatree.loop.scanners.base import ScanSignal

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IdleStackReaperScanner:
    """Emit ``local_stack.reap_idle`` for each idle running worktree of *overlay*."""

    overlay: str
    idle_minutes: int = 30
    cadence_minutes: int = 5
    name: str = "idle_stack_reaper"

    def scan(self) -> list[ScanSignal]:
        from teatree.core.models.local_stack_reaper_marker import LocalStackReaperMarker  # noqa: PLC0415

        try:
            marker = LocalStackReaperMarker.load()
        except Exception:
            logger.exception("idle_stack_reaper: could not load marker — skipping tick")
            return []
        if self._cadence_blocks(marker):
            return []
        try:
            candidates = list(reapable_worktrees(overlay=self.overlay, idle_minutes=self.idle_minutes))
        except Exception:
            logger.exception("idle_stack_reaper: reapable_worktrees failed — skipping tick")
            return []
        try:
            marker.stamp_run()
        except Exception:
            logger.exception("idle_stack_reaper: failed to stamp marker run")
        return [
            ScanSignal(
                kind="local_stack.reap_idle",
                summary=f"idle stack {wt.repo_path} ({wt.branch}) — stopping to free a slot",
                payload={
                    "worktree_id": wt.pk,
                    "overlay": self.overlay,
                    "repo_path": wt.repo_path,
                    "branch": wt.branch,
                },
            )
            for wt in candidates
        ]

    def _cadence_blocks(self, marker: object) -> bool:
        """True iff the reaper cadence has not yet elapsed since ``last_run_at``."""
        last_run = getattr(marker, "last_run_at", None)
        if last_run is None:
            return False
        elapsed_minutes = (timezone.now() - last_run).total_seconds() / 60.0
        return elapsed_minutes < self.cadence_minutes


__all__ = ["IdleStackReaperScanner"]
