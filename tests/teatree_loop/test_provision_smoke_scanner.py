"""Tests for the provision-smoke scanner (#1308).

Mirrors the shape of the architectural-review and scanning-news
scanners — cadence-driven, queues a single ``dogfood_smoke`` task per
tick when the cadence has elapsed, returns no signal when a prior task
is still in-flight.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.config import UserSettings
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.loop.scanners.provision_smoke import DOGFOOD_SMOKE_PHASE, ProvisionSmokeScanner

TEST_OVERLAY_NAME = "t3-teatree"


def _scanner(*, cadence_hours: int = 24) -> ProvisionSmokeScanner:
    return ProvisionSmokeScanner(
        overlay_name=TEST_OVERLAY_NAME,
        cadence_hours=cadence_hours,
    )


def _last_smoke_task() -> Task | None:
    return (
        Task.objects.filter(
            ticket__overlay=TEST_OVERLAY_NAME,
            phase=DOGFOOD_SMOKE_PHASE,
        )
        .order_by("-id")
        .first()
    )


def _backdate_task(task: Task, *, hours: int) -> None:
    Session.objects.filter(pk=task.session_id).update(
        started_at=timezone.now() - timedelta(hours=hours),
    )


class ProvisionSmokeScannerTests(TestCase):
    def test_bootstrap_first_run_queues_task(self) -> None:
        signals = _scanner().scan()

        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "dogfood_smoke.queued"
        assert signal.payload["overlay"] == TEST_OVERLAY_NAME
        assert signal.payload["phase"] == DOGFOOD_SMOKE_PHASE
        assert signal.payload["trigger"] == "bootstrap"

        task = _last_smoke_task()
        assert task is not None
        assert task.phase == DOGFOOD_SMOKE_PHASE
        assert task.status == Task.Status.PENDING

    def test_fresh_timestamp_blocks_new_task(self) -> None:
        """A recent prior run inside the cadence window suppresses new queueing."""
        _scanner().scan()
        prior = _last_smoke_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        _backdate_task(prior, hours=1)  # 1 hour ago — far inside the 24h window

        second = _scanner().scan()

        assert second == []
        latest = _last_smoke_task()
        assert latest is not None
        assert latest.pk == prior.pk

    def test_stale_timestamp_fires_smoke_again(self) -> None:
        """A prior run older than cadence_hours triggers a new task."""
        _scanner().scan()
        prior = _last_smoke_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        _backdate_task(prior, hours=25)  # past the 24h cadence

        second = _scanner().scan()

        assert len(second) == 1
        assert second[0].payload["trigger"] == "cadence"
        new_task = _last_smoke_task()
        assert new_task is not None
        assert new_task.pk != prior.pk

    def test_pending_task_blocks_dupes_even_when_cadence_elapsed(self) -> None:
        """An in-flight PENDING task is the lock — no dupes regardless of cadence."""
        _scanner().scan()
        prior = _last_smoke_task()
        assert prior is not None
        _backdate_task(prior, hours=48)  # cadence WOULD trigger, but task is still PENDING

        second = _scanner().scan()

        assert second == []

    def test_claimed_task_blocks_dupes(self) -> None:
        """A CLAIMED (in-flight) task suppresses the next scan."""
        _scanner().scan()
        prior = _last_smoke_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.CLAIMED)
        _backdate_task(prior, hours=48)

        assert _scanner().scan() == []

    def test_empty_overlay_name_returns_no_signal(self) -> None:
        """Defensive — an unconfigured overlay name produces no signal, no task."""
        scanner = ProvisionSmokeScanner(overlay_name="")
        assert scanner.scan() == []
        assert _last_smoke_task() is None


class OperProvisionSmokeWiringTests(TestCase):
    """Confirm the tick-job builder reads core config + active overlay (#1308)."""

    def _patched_settings(self, **overrides: object) -> UserSettings:
        return UserSettings(**overrides)

    def test_default_core_config_builds_scanner(self) -> None:
        from teatree.loop.tick_jobs import _dogfood_smoke_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.tick_jobs.load_config",
            return_value=type("Cfg", (), {"user": self._patched_settings()})(),
        ):
            scanner = _dogfood_smoke_scanner()

        assert scanner is not None
        assert scanner.cadence_hours == 24

    def test_disabled_in_core_config_skips_wiring(self) -> None:
        from teatree.loop.tick_jobs import _dogfood_smoke_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.tick_jobs.load_config",
            return_value=type(
                "Cfg",
                (),
                {"user": self._patched_settings(dogfood_smoke_disabled=True)},
            )(),
        ):
            scanner = _dogfood_smoke_scanner()

        assert scanner is None

    def test_custom_cadence_propagates_through_wiring(self) -> None:
        from teatree.loop.tick_jobs import _dogfood_smoke_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.tick_jobs.load_config",
            return_value=type(
                "Cfg",
                (),
                {"user": self._patched_settings(dogfood_smoke_cadence_hours=12)},
            )(),
        ):
            scanner = _dogfood_smoke_scanner()

        assert scanner is not None
        assert scanner.cadence_hours == 12


class ScannerProtocolTests(TestCase):
    def test_scanner_name_is_stable_for_dispatch_routing(self) -> None:
        """The scanner's ``name`` is the dispatch key — it must not drift."""
        scanner = ProvisionSmokeScanner(overlay_name=TEST_OVERLAY_NAME)
        assert scanner.name == "provision_smoke"
