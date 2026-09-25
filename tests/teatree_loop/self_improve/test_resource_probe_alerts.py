from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import SelfImproveFiring
from teatree.loop.scanners.resource_pressure import ResourceReading
from teatree.loop.self_improve.detectors.base import ActionRung, DetectorReport
from teatree.loop.self_improve.detectors.pressure_incident import PressureIncidentDetector
from teatree.loop.self_improve.schedule import DeliveryRoutes, run_tier


class ResourceProbeAlertTests(TestCase):
    def test_inert_ram_retries_deduplicates_and_recurs(self) -> None:
        with (
            TemporaryDirectory() as directory,
            patch(
                "teatree.loop.self_improve.detectors.pressure_incident.cgroup_memory_probe_inert", return_value=False
            ),
        ):
            _exercise_alert(Path(directory), "ram")

    def test_inert_disk_retries_deduplicates_and_recurs(self) -> None:
        with (
            TemporaryDirectory() as directory,
            patch(
                "teatree.loop.self_improve.detectors.pressure_incident.cgroup_memory_probe_inert", return_value=False
            ),
        ):
            _exercise_alert(Path(directory), "disk")


def _exercise_alert(tmp_path: Path, resource: str) -> None:
    reading = ResourceReading(disk_free_gb=100.0, ram_avail_gb=20.0)
    broken = ResourceReading(
        disk_free_gb=None if resource == "disk" else 100.0,
        ram_avail_gb=None if resource == "ram" else 20.0,
    )
    detector = PressureIncidentDetector(directory=tmp_path, read_resources=lambda: reading)
    delivered = False
    attempts: list[str] = []

    def alert(report: DetectorReport, _existing: SelfImproveFiring | None) -> bool:
        attempts.append(report.dedup_key)
        return delivered

    routes = DeliveryRoutes(owner_alert=alert, overlay_name="t3-teatree")
    assert run_tier("cheap", delivery=routes, detectors=[detector]).reports == []
    reading = broken
    failed = run_tier("cheap", delivery=routes, detectors=[detector])
    assert len(failed.reports) == 1
    assert failed.reports[0].payload["kind"] == f"{resource}_probe_inert"
    assert failed.actions == []
    assert not SelfImproveFiring.objects.exists()

    delivered = True
    success = run_tier("cheap", delivery=routes, detectors=[detector])
    assert len(success.actions) == 1
    assert success.actions[0].rung == ActionRung.SLACK
    assert run_tier("cheap", delivery=routes, detectors=[detector]).actions == []
    assert len(attempts) == 2

    reading = ResourceReading(disk_free_gb=100.0, ram_avail_gb=20.0)
    assert run_tier("cheap", delivery=routes, detectors=[detector]).reports == []
    assert SelfImproveFiring.objects.get().resolved_at is not None
    reading = broken
    assert len(run_tier("cheap", delivery=routes, detectors=[detector]).actions) == 1
    assert len(attempts) == 3
