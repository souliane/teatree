import datetime as dt
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ScannerFault, SelfImproveFiring
from teatree.loop.self_improve import ActionRung, BudgetVerdict, Tier, run_tier
from teatree.loop.self_improve.detectors.loop_health import LoopHealthDetector
from teatree.loop.self_improve.schedule import DeliveryRoutes
from teatree.loops.loop_staleness import Admission, DeadlinedLoop, LoopHealth, StaleLoop


def _reading(
    *,
    stale: bool = False,
    deadlined: bool = False,
    driverless: bool = False,
    fleet_admits: bool = True,
    considered: int = 2,
) -> LoopHealth:
    return LoopHealth(
        admission=Admission(mode="present", source="test", admitted=(), admitted_total=1),
        stale=(StaleLoop(name="dispatch", cadence_seconds=60, age_seconds=500, ever_ran=True, suppressed=False),)
        if stale
        else (),
        considered=considered,
        driverless=("orphan",) if driverless else (),
        fleet_admits=fleet_admits,
        deadlined=(DeadlinedLoop(name="review", kills=2, deadline_seconds=30, backoff_seconds=60),)
        if deadlined
        else (),
    )


class LoopHealthDetectorTests(TestCase):
    def test_missing_reader_is_incomplete_and_does_not_close_loop_incident(self) -> None:
        detector = LoopHealthDetector(read_health=lambda _now: _reading(stale=True))
        run_tier(
            Tier.CHEAP,
            detectors=[detector],
            budget=BudgetVerdict.allow(),
            delivery=DeliveryRoutes(overlay_name="t3-teatree"),
        )
        unknown = LoopHealthDetector().detect_checked()
        assert unknown.complete is False
        assert unknown.reason == "loop_health_unavailable"
        run_tier(Tier.CHEAP, detectors=[LoopHealthDetector()], budget=BudgetVerdict.allow())
        assert SelfImproveFiring.objects.filter(detector="loop_health", resolved_at__isnull=True).exists()

    def test_loop_categories_open_tickets_and_clean_reading_resolves(self) -> None:
        reading = _reading(stale=True, deadlined=True, driverless=True)
        detector = LoopHealthDetector(read_health=lambda _now: reading)
        result = run_tier(
            Tier.CHEAP,
            detectors=[detector],
            budget=BudgetVerdict.allow(),
            delivery=DeliveryRoutes(overlay_name="t3-teatree"),
        )
        assert len(result.actions) == 3
        assert all(action.rung == ActionRung.TICKET and action.firing.ticket is not None for action in result.actions)
        clean = LoopHealthDetector(read_health=lambda _now: _reading())
        run_tier(Tier.CHEAP, detectors=[clean], budget=BudgetVerdict.allow())
        assert SelfImproveFiring.objects.filter(detector=detector.name, resolved_at__isnull=True).count() == 0

    def test_unreadable_reader_keeps_loop_firing_open_but_scans_scanner_faults(self) -> None:
        detector = LoopHealthDetector(read_health=lambda _now: _reading(stale=True))
        run_tier(
            Tier.CHEAP,
            detectors=[detector],
            budget=BudgetVerdict.allow(),
            delivery=DeliveryRoutes(overlay_name="t3-teatree"),
        )
        ScannerFault.objects.record_failure("scanner[overlay]", "auth", "denied")
        scanner_seen = run_tier(
            Tier.CHEAP,
            detectors=[LoopHealthDetector(read_health=lambda _now: _reading(stale=True))],
            budget=BudgetVerdict.allow(),
            delivery=DeliveryRoutes(overlay_name="t3-teatree"),
        )
        assert any("scanner::scanner[overlay]" in report.dedup_key for report in scanner_seen.reports)
        ScannerFault.objects.heal("scanner[overlay]")

        def unreadable(_now: dt.datetime) -> LoopHealth:
            raise OSError

        result = run_tier(
            Tier.CHEAP,
            detectors=[LoopHealthDetector(read_health=unreadable)],
            budget=BudgetVerdict.allow(),
            delivery=DeliveryRoutes(overlay_name="t3-teatree"),
        )
        assert result.degraded_scans == [("loop_health", "loop_health_unreadable")]
        assert not any("scanner::scanner[overlay]" in report.dedup_key for report in result.reports)
        assert SelfImproveFiring.objects.filter(
            detector="loop_health", dedup_key__contains="loop::", resolved_at__isnull=True
        ).exists()
        assert SelfImproveFiring.objects.filter(
            detector="loop_health", dedup_key__contains="scanner::scanner[overlay]", resolved_at__isnull=False
        ).exists()

    def test_old_or_healed_scanner_faults_do_not_report_and_frozen_requires_admission(self) -> None:
        ScannerFault.objects.record_failure("old", "auth", "denied")
        ScannerFault.objects.filter(scanner="old").update(last_failed_at=timezone.now() - dt.timedelta(days=2))
        ScannerFault.objects.record_failure("healed", "auth", "denied")
        ScannerFault.objects.heal("healed")
        with patch("django.utils.timezone.now", return_value=timezone.now()):
            reports = LoopHealthDetector(read_health=lambda _now: _reading()).detect()
        assert reports == []
        frozen = LoopHealthDetector(read_health=lambda _now: _reading(stale=True, considered=1)).detect()
        assert any("frozen_fleet" in report.dedup_key for report in frozen)
        frozen = LoopHealthDetector(
            read_health=lambda _now: _reading(stale=True, considered=1, fleet_admits=False)
        ).detect()
        assert not any("frozen" in report.dedup_key for report in frozen)
