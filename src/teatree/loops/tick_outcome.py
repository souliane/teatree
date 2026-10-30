"""Detect a periodic loop that completed a tick without a task or named reason."""

import logging
from typing import TYPE_CHECKING

from teatree.core.factory.health_signal import HealthSignal
from teatree.core.models.known_issue import KnownIssue

if TYPE_CHECKING:
    from teatree.loop.tick import TickReport

logger = logging.getLogger(__name__)


def periodic_tick_is_silent(loop: str, *, jobs_built: bool, report: "TickReport") -> bool:
    fingerprint = f"loop-silent:{loop}"
    if report.errors:
        return False
    if any(signal.kind.endswith(".queued") and signal.payload.get("task_id") for signal in report.signals):
        KnownIssue.objects.resolve_fingerprint(fingerprint)
        return False
    if any(isinstance(signal.payload.get("reason"), str) and signal.payload["reason"] for signal in report.signals):
        KnownIssue.objects.resolve_fingerprint(fingerprint)
        return False
    reason = "no signal after scanner job" if jobs_built else "no scanner job was built"
    summary = f"periodic loop {loop} returned without a task or named reason: {reason}"
    KnownIssue.objects.record_reactive(
        HealthSignal(
            fingerprint=fingerprint,
            severity=KnownIssue.Severity.WARNING,
            kind="loop_silent",
            summary=summary,
        )
    )
    logger.warning("%s", summary)
    return True
