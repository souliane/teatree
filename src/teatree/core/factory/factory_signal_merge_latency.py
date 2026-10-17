"""CLEAR-to-merge latency and the actionable CLEAR backlog companion."""

from datetime import datetime
from statistics import median

from teatree.core.factory.factory_signal_queries import (
    MIN_SAMPLE,
    Computation,
    S4Evidence,
    SignalReading,
    SignalStatus,
    Window,
    _merge_audits_in,
)
from teatree.core.factory.merge_backlog import STALE_CLEAR_HOURS, max_actionable_clear_age_hours


def compute_s4(window: Window, overlay: str, now: datetime) -> Computation:
    audits = _merge_audits_in(window, overlay)
    latencies = [
        (audit.merged_at - audit.clear.issued_at).total_seconds() / 3600.0
        for audit in audits
        if audit.merged_at >= audit.clear.issued_at
    ]
    denom = len(latencies)
    stale_hours = max_actionable_clear_age_hours(overlay, now)
    hard_red = stale_hours is not None and stale_hours > STALE_CLEAR_HOURS
    evidence: S4Evidence = {
        "merges": denom,
        "stale_clear_hours": round(stale_hours, 2) if stale_hours is not None else 0.0,
    }
    reason = "actionable CLEAR older than 48h" if hard_red else ""
    if denom < MIN_SAMPLE:
        return Computation(
            SignalReading(0.0, denom, window.days, SignalStatus.INSUFFICIENT_DATA),
            evidence,
            hard_red=hard_red,
            hard_red_reason=reason,
        )
    return Computation(
        SignalReading(round(median(latencies), 3), denom, window.days, SignalStatus.OK),
        evidence,
        hard_red=hard_red,
        hard_red_reason=reason,
    )
