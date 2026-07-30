"""Local-stack queue drainer scanner — drains the acquisition queue (souliane/teatree#2190, #44).

A global (``overlay=""``) mechanical scanner. Each tick it asks
:meth:`LocalStackQueueItemManager.due_for_attempt` for the queued/retrying
acquisition requests whose Fibonacci-minute backoff has elapsed and emits one
``local_stack.queue_acquire`` signal per due item. The paired mechanical
handler (``drain_stack_queue_item``) re-checks the cap and either re-fires
``Worktree.start_services`` (slot freed → ``READY``) or reschedules the next
Fibonacci attempt — it NEVER tears down another ticket's stack.

The per-item backoff IS the cadence (carried on the row's ``next_attempt_at``),
so this scanner needs no marker. Best-effort: a DB error logs and returns an
empty list rather than crashing the tick.
"""

import logging
from dataclasses import dataclass

from teatree.loop.scanners.base import ScanSignal

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LocalStackQueueDrainerScanner:
    """Emit ``local_stack.queue_acquire`` for each due queue item of *overlay*."""

    overlay: str
    name: str = "local_stack_queue_drainer"

    def scan(self) -> list[ScanSignal]:
        from teatree.core.models.local_stack_queue import LocalStackQueueItem  # noqa: PLC0415 — lazy ORM import

        try:
            due = list(
                LocalStackQueueItem.objects.due_for_attempt().filter(overlay=self.overlay).select_related("worktree"),
            )
        except Exception:
            logger.exception("local_stack_queue_drainer: due_for_attempt query failed — skipping tick")
            return []
        return [
            ScanSignal(
                kind="local_stack.queue_acquire",
                summary=f"draining queued stack acquire (wt {item.worktree_id}, attempt {item.attempt_count})",
                payload={
                    "queue_item_id": item.pk,
                    "worktree_id": item.worktree_id,
                    "overlay": self.overlay,
                    "attempt_count": item.attempt_count,
                },
            )
            for item in due
        ]


__all__ = ["LocalStackQueueDrainerScanner"]
