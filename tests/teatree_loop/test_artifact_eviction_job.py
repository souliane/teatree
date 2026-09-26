"""Artifact eviction is its OWN job, reachable without disk pressure (#4244).

The reclaim loses nothing at any fullness, so gating it on the disk-CRIT band only
delayed it — and that band could never fire anyway while the scanner measured the
container's rootfs. These tests pin the decoupling: the sweep emits on a healthy box,
routes to its own mechanical handler, and stamps a marker field the destructive
ladder's anti-thrash gate does not read.
"""

import datetime as _dt
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.config import UserSettings
from teatree.core.models.resource_pressure_marker import ResourcePressureMarker
from teatree.loop.dispatch import dispatch
from teatree.loop.scanners.artifact_eviction import ArtifactEvictionScanner
from teatree.loop.scanners.resource_pressure import ResourcePressureScanner

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_PRESSURE = "teatree.loop.scanners.resource_pressure"
_SIGNAL = "resource.artifacts_reclaimable"


class ReachableWithoutDiskPressureTests(TestCase):
    """THE decoupling. A healthy box still reclaims; the pressure scanner stays silent."""

    def test_the_sweep_emits_while_the_pressure_ladder_says_nothing(self) -> None:
        with (
            patch(f"{_PRESSURE}.read_disk_free_gb", return_value=400.0),
            patch(f"{_PRESSURE}.read_ram_avail_gb", return_value=64.0),
        ):
            pressure = ResourcePressureScanner().scan()
        artifacts = ArtifactEvictionScanner().scan()

        assert pressure == [], "400 GB free and 64 GB RAM is not a pressure event"
        assert [signal.kind for signal in artifacts] == [_SIGNAL]

    def test_the_signal_carries_the_retention_window(self) -> None:
        signals = ArtifactEvictionScanner(artifact_idle_days=7.0).scan()
        assert signals[0].payload["artifact_idle_days"] == pytest.approx(7.0)


class CadenceTests(TestCase):
    def test_a_recent_sweep_short_circuits_the_scan(self) -> None:
        marker = ResourcePressureMarker.load()
        marker.last_artifact_sweep_at = timezone.now()
        marker.save(update_fields=["last_artifact_sweep_at"])

        assert ArtifactEvictionScanner(cadence_minutes=30).scan() == []

    def test_an_elapsed_cadence_emits_again(self) -> None:
        marker = ResourcePressureMarker.load()
        marker.last_artifact_sweep_at = timezone.now() - _dt.timedelta(minutes=31)
        marker.save(update_fields=["last_artifact_sweep_at"])

        assert [s.kind for s in ArtifactEvictionScanner(cadence_minutes=30).scan()] == [_SIGNAL]

    def test_the_pressure_ladders_own_stamp_does_not_gate_this_sweep(self) -> None:
        """``last_freed_at`` is the destructive ladder's gate — reading it here would couple them."""
        marker = ResourcePressureMarker.load()
        marker.last_freed_at = timezone.now()
        marker.save(update_fields=["last_freed_at"])

        assert [s.kind for s in ArtifactEvictionScanner().scan()] == [_SIGNAL]


class WiringTests(TestCase):
    def test_the_signal_routes_to_its_own_mechanical_handler(self) -> None:
        actions = dispatch([ArtifactEvictionScanner().scan()[0]])
        assert len(actions) == 1
        assert (actions[0].kind, actions[0].zone) == ("mechanical", "sweep_artifacts")

    def test_the_handler_table_resolves_that_zone(self) -> None:
        from teatree.loop.mechanical import HANDLERS  # noqa: PLC0415 — deferred: heavy import

        assert "sweep_artifacts" in HANDLERS

    def test_the_mini_loop_builds_the_job(self) -> None:
        from teatree.loops.resource_pressure.loop import _build_jobs  # noqa: PLC0415 — deferred: tick-time import

        scanners = [job.scanner for job in _build_jobs()]
        assert any(isinstance(scanner, ArtifactEvictionScanner) for scanner in scanners)
        assert any(isinstance(scanner, ResourcePressureScanner) for scanner in scanners)

    def test_the_factory_threads_the_idle_window_through(self) -> None:
        from teatree.loop.global_scanner_factories import _artifact_eviction_scanner  # noqa: PLC0415 — deferred

        settings = UserSettings(artifact_idle_days=9.0, disk_warn_free_gb=30.0, disk_crit_free_gb=12.0)
        with patch("teatree.loop.global_scanner_factories.get_effective_settings", return_value=settings):
            scanner = _artifact_eviction_scanner()
        assert scanner.artifact_idle_days == pytest.approx(9.0)
        assert scanner.disk_warn_free_gb == pytest.approx(30.0)
        assert scanner.disk_crit_free_gb == pytest.approx(12.0)


class PressureDecayTests(TestCase):
    """The retention window is a CEILING the measured disk decays (#4644).

    A fixed window was the sweep's only size-relevant criterion, so a checkout some other
    process rewrote hourly was ineligible on every pass forever. These pin the whole path:
    the scanner carries the reading and the two thresholds, and the job hands the decayed
    value — ``None`` below the floor — to the planner.
    """

    def test_the_signal_carries_the_reading_and_the_two_thresholds(self) -> None:
        marker = ResourcePressureMarker.load()
        marker.last_disk_free_gb = 4.0
        marker.save(update_fields=["last_disk_free_gb"])

        payload = ArtifactEvictionScanner(disk_warn_free_gb=25.0, disk_crit_free_gb=10.0).scan()[0].payload

        assert payload["free_gb"] == pytest.approx(4.0)
        assert payload["disk_warn_free_gb"] == pytest.approx(25.0)
        assert payload["disk_crit_free_gb"] == pytest.approx(10.0)

    def test_below_the_critical_floor_age_stops_gating_the_planner(self) -> None:
        from teatree.loop import mechanical_artifacts  # noqa: PLC0415 — deferred

        with patch.object(mechanical_artifacts, "plan_artifact_eviction") as planner:
            mechanical_artifacts._surveyed_artifacts(
                {"artifact_idle_days": 2.0, "free_gb": 0.2, "disk_warn_free_gb": 25.0, "disk_crit_free_gb": 10.0},
            )

        assert planner.call_args.kwargs["idle_days"] is None

    def test_a_comfortable_disk_hands_the_planner_the_configured_window(self) -> None:
        from teatree.loop import mechanical_artifacts  # noqa: PLC0415 — deferred

        with patch.object(mechanical_artifacts, "plan_artifact_eviction") as planner:
            mechanical_artifacts._surveyed_artifacts(
                {"artifact_idle_days": 2.0, "free_gb": 200.0, "disk_warn_free_gb": 25.0, "disk_crit_free_gb": 10.0},
            )

        assert planner.call_args.kwargs["idle_days"] == pytest.approx(2.0)

    def test_an_unmeasured_box_never_relaxes_the_window(self) -> None:
        """``last_disk_free_gb`` is ``None`` until the pressure scanner has run once."""
        from teatree.loop import mechanical_artifacts  # noqa: PLC0415 — deferred

        with patch.object(mechanical_artifacts, "plan_artifact_eviction") as planner:
            mechanical_artifacts._surveyed_artifacts(
                {"artifact_idle_days": 2.0, "free_gb": None, "disk_warn_free_gb": 25.0, "disk_crit_free_gb": 10.0},
            )

        assert planner.call_args.kwargs["idle_days"] == pytest.approx(2.0)


class NoEnableFlagTests(TestCase):
    """The sweep is unconditional, and the absence of a switch is the design (#4244).

    A deletion is authorised by PROOF — rebuild inputs present, no live process, no symlink
    resolving at it, and an enumeration complete enough to say so — with anything unprovable
    kept silently. A flag in front of that proves nothing extra, and an off-by-default one
    only guarantees the reclaim never happens on the box that needed it.
    """

    def test_no_toggle_exists_to_turn_the_pass_off(self) -> None:
        assert not hasattr(UserSettings(), "artifact_eviction_enabled")

    def test_the_scanner_is_always_built(self) -> None:
        from teatree.loop.global_scanner_factories import _artifact_eviction_scanner  # noqa: PLC0415 — deferred

        assert _artifact_eviction_scanner() is not None

    def test_the_idle_window_survives_as_a_tuning_knob(self) -> None:
        """Removing the switch must not take the retention window with it."""
        assert UserSettings().artifact_idle_days == pytest.approx(2.0)


class MarkerCouplingTests(TestCase):
    """These pin the marker COUPLING: which field the sweep stamps, and whose plan it writes."""

    def test_the_sweep_stamps_its_own_field_and_never_the_ladders(self) -> None:
        """Stamping ``last_freed_at`` would rate-limit the real ladder — silently, per #4244."""
        from teatree.loop.mechanical_artifacts import sweep_artifacts  # noqa: PLC0415 — deferred

        marker = ResourcePressureMarker.load()
        assert marker.last_freed_at is None, "the control: nothing has stamped it"

        sweep_artifacts({"artifact_idle_days": 2.0})

        marker.refresh_from_db()
        assert marker.last_freed_at is None, "a loss-free sweep must not trip the anti-thrash gate"
        assert marker.last_artifact_sweep_at is not None

    def test_the_sweep_keeps_its_own_plan_and_never_clobbers_the_ladders(self) -> None:
        """One shared TextField meant only whichever pass ran last had a surviving account.

        The two run on different cadences on the same mini-loop, so the destructive
        ladder's record — including what its delete-time guard stopped, which is the whole
        point of reporting it — was overwritten by every 30-minute artifact sweep.
        """
        from teatree.loop.mechanical_artifacts import sweep_artifacts  # noqa: PLC0415 — deferred

        ladder_plan = "the destructive ladder's account of what it deleted"
        marker = ResourcePressureMarker.load()
        marker.last_plan = ladder_plan
        marker.save(update_fields=["last_plan"])

        sweep_artifacts({"artifact_idle_days": 2.0})

        marker.refresh_from_db()
        assert marker.last_plan == ladder_plan, "the artifact sweep overwrote the ladder's plan"
        assert "resource=artifacts" in marker.last_artifact_plan, marker.last_artifact_plan
