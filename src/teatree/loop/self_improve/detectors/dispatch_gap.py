"""``DispatchGapDetector`` — "tasks waiting, no agents working" smell.

Claimable PENDING work has sat for a whole stall window with nothing claimed and no real
attempt run: the worker is not draining the queue. The progress anchor is
:func:`teatree.core.factory.queue_stall.read_queue_stall`, the same one ``t3 doctor check``
FAILs on. A worker-lock probe would not do: the detector runs inside the worker.

Read-only by design — a warn-level firing whose ladder ceiling is ``ticket``, with
``auto_fix`` off, since picking the remedy is a judgment call.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar

from django.db.models import Q
from django.utils import timezone

from teatree.core.admission.dispatch_mask import headless_admission_block_reason
from teatree.core.factory.queue_stall import read_queue_stall, stall_minutes
from teatree.core.models.task import Task
from teatree.core.telemetry.admission import latest_admission_reason
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.self_improve.dedup import canonical_key, state_hash
from teatree.loop.self_improve.detectors.base import ActionRung, DetectorReport


def _admission_reason() -> str:
    try:
        return latest_admission_reason() or ""
    except Exception:  # noqa: BLE001 — diagnostic only; missing telemetry must not hide the stall
        return ""


@dataclass(slots=True)
class DispatchGapDetector:
    """Claimable work nobody has claimed or run for a whole stall window."""

    name: ClassVar[str] = "dispatch_gap"
    tier: ClassVar[str] = "cheap"
    severity: ClassVar[str] = "warn"
    max_rung: ClassVar[str] = ActionRung.TICKET
    auto_fix: ClassVar[bool] = False

    def detect(self) -> list[DetectorReport]:
        # A masked or blocked dispatch leaves claimable rows sitting by design.
        if headless_admission_block_reason():
            return []
        now = timezone.now()
        minutes = stall_minutes()
        # A park that lifted inside the window has not yet had a whole window to be claimed.
        candidates = (
            Task.objects.claimable()
            .filter(status=Task.Status.PENDING)
            .filter(Q(not_before__isnull=True) | Q(not_before__lte=now - timedelta(minutes=minutes)))
        )
        stall = read_queue_stall(candidates, now=now, minutes=minutes)
        if stall is None:
            return []
        age_minutes = int((now - stall.oldest_created_at).total_seconds() // 60)
        return [
            DetectorReport(
                detector=self.name,
                dedup_key=canonical_key(self.name, "global"),
                state_hash=state_hash("stalled", stall.oldest_pk),
                severity=self.severity,
                max_rung=self.max_rung,
                summary=(
                    f"{stall.pending} claimable task(s) unclaimed, oldest waiting {age_minutes}m, "
                    f"nothing claimed or run for {minutes}m"
                ),
                payload={
                    "pending_count": stall.pending,
                    "oldest_age_minutes": age_minutes,
                    "admission_reason": _admission_reason(),
                },
                auto_fix=self.auto_fix,
            )
        ]

    def scan(self) -> list[ScanSignal]:
        return [report.to_signal() for report in self.detect()]
