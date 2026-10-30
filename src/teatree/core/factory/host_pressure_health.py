"""Health signal for the machine-pressure feed."""

from teatree.core.factory.health_signal import HealthSignal, SignalCollection
from teatree.core.models.known_issue import KnownIssue
from teatree.utils.host_pressure import feed_fault


def host_pressure_signals() -> SignalCollection:
    fault = feed_fault()
    if fault is None:
        return SignalCollection()
    return SignalCollection(
        (
            HealthSignal(
                fingerprint="host-pressure-feed",
                severity=KnownIssue.Severity.WARNING,
                kind="host_pressure_feed",
                summary=f"host pressure {fault}; admission uses the container reading",
            ),
        )
    )
