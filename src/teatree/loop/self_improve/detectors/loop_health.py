"""Loop fleet and scanner failure incidents for the cheap monitor tier."""

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Protocol

from django.utils import timezone

from teatree.core.models.scanner_fault import ScannerFault
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.self_improve.dedup import canonical_key, state_hash
from teatree.loop.self_improve.detectors.base import ActionRung, DetectorReport, DetectorScan

logger = logging.getLogger(__name__)


class StaleReading(Protocol):
    @property
    def name(self) -> str: ...


class DeadlinedReading(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def kills(self) -> int: ...


class HealthReading(Protocol):
    @property
    def unexplained(self) -> tuple[StaleReading, ...]: ...

    @property
    def deadlined(self) -> tuple[DeadlinedReading, ...]: ...

    @property
    def driverless(self) -> tuple[str, ...]: ...

    @property
    def frozen_fleet(self) -> bool: ...

    @property
    def fleet_admits(self) -> bool: ...


@dataclass(slots=True)
class LoopHealthDetector:
    name: ClassVar[str] = "loop_health"
    tier: ClassVar[str] = "cheap"
    severity: ClassVar[str] = "error"
    max_rung: ClassVar[str] = ActionRung.TICKET
    auto_fix: ClassVar[bool] = False
    always_on: ClassVar[bool] = True

    read_health: Callable[[dt.datetime], HealthReading] | None = None

    def detect(self) -> list[DetectorReport]:
        return self.detect_checked().reports

    def scan(self) -> list[ScanSignal]:
        return [report.to_signal() for report in self.detect()]

    def _loop_report(self, category: str, names: tuple[str, ...]) -> DetectorReport:
        return DetectorReport(
            detector=self.name,
            dedup_key=canonical_key(self.name, f"loop::{category}"),
            state_hash=state_hash(*names),
            severity=self.severity,
            max_rung=self.max_rung,
            summary=f"Loop {category}: {', '.join(names)}",
            payload={"kind": f"loop_{category}", "loops": names},
            requested_rung=ActionRung.TICKET,
        )

    def detect_checked(self) -> DetectorScan:
        now = timezone.now()
        reports = [
            DetectorReport(
                detector=self.name,
                dedup_key=canonical_key(self.name, f"scanner::{fault.scanner}"),
                state_hash=state_hash(fault.error_class),
                severity=self.severity,
                max_rung=self.max_rung,
                summary=f"Scanner {fault.scanner} failing: {fault.error_class}: {fault.detail}",
                payload={"kind": "scanner_fault", "scanner": fault.scanner, "error_class": fault.error_class},
                requested_rung=ActionRung.TICKET,
            )
            for fault in ScannerFault.objects.recent_open(now - dt.timedelta(days=1))
        ]
        loop_prefix = canonical_key(self.name, "loop::")
        if self.read_health is None:
            return DetectorScan(
                reports,
                complete=False,
                reason="loop_health_unavailable",
                protected_prefixes=(loop_prefix,),
            )
        try:
            health = self.read_health(now)
        except Exception:
            logger.exception("Loop health reading failed")
            return DetectorScan(
                reports,
                complete=False,
                reason="loop_health_unreadable",
                protected_prefixes=(loop_prefix,),
            )
        if health.unexplained:
            reports.append(self._loop_report("unexplained_stale", tuple(row.name for row in health.unexplained)))
        if health.deadlined:
            reports.append(self._loop_report("deadlined", tuple(row.name for row in health.deadlined)))
        if health.driverless:
            reports.append(self._loop_report("driverless", health.driverless))
        if health.frozen_fleet and health.fleet_admits:
            reports.append(self._loop_report("frozen_fleet", ("all measured loops",)))
        return DetectorScan(reports)
