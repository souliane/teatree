"""The global operational-health aggregator (PR-17, M6).

Distinct from ``teatree.core.health`` (per-worktree readiness). This module
computes the green/yellow/red factory-health verdict from deterministic durable
signals and persists them as ``KnownIssue`` rows.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.apps import apps
from django.utils import timezone

from teatree.core.models.known_issue import KnownIssue
from teatree.core.operational_health import (
    HealthSignal,
    HealthStatus,
    _overlay_health_signals,
    _stale_tick_signals,
    _status_from_issues,
    read_health,
    reconcile_health,
)

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_OVERLAY_ON_FIRE = "overlay is on fire"


def _issue(severity: str) -> KnownIssue:
    return KnownIssue(fingerprint=f"x:{severity}:{timezone.now().timestamp()}", severity=severity, summary="s")


class TestStatusThresholds:
    def test_no_issues_is_green(self) -> None:
        assert _status_from_issues([]) is HealthStatus.GREEN

    def test_one_warning_is_yellow(self) -> None:
        assert _status_from_issues([_issue(KnownIssue.Severity.WARNING)]) is HealthStatus.YELLOW

    def test_three_warnings_is_red(self) -> None:
        warnings = [_issue(KnownIssue.Severity.WARNING) for _ in range(3)]
        assert _status_from_issues(warnings) is HealthStatus.RED

    def test_two_warnings_stays_yellow(self) -> None:
        warnings = [_issue(KnownIssue.Severity.WARNING) for _ in range(2)]
        assert _status_from_issues(warnings) is HealthStatus.YELLOW

    def test_any_critical_is_red(self) -> None:
        assert _status_from_issues([_issue(KnownIssue.Severity.CRITICAL)]) is HealthStatus.RED


class TestReadHealth:
    def test_reads_open_issues_only(self) -> None:
        KnownIssue.objects.record_signal(HealthSignal("a", KnownIssue.Severity.WARNING, "a"))
        row_b = KnownIssue.objects.record_signal(HealthSignal("b", KnownIssue.Severity.WARNING, "b"))
        KnownIssue.objects.dismiss(row_b.pk)
        report = read_health()
        assert report.status is HealthStatus.YELLOW
        assert report.open_count == 1


class TestReconcileHealth:
    def test_reconcile_persists_and_resolves(self) -> None:
        signals = [
            HealthSignal("sig-a", KnownIssue.Severity.WARNING, "a"),
            HealthSignal("sig-b", KnownIssue.Severity.CRITICAL, "b"),
        ]
        with patch("teatree.core.operational_health.collect_signals", return_value=signals):
            report = reconcile_health()
        assert report.status is HealthStatus.RED
        assert report.open_count == 2
        # Next reconcile with sig-a gone auto-resolves it, leaves the critical.
        with patch("teatree.core.operational_health.collect_signals", return_value=signals[1:]):
            report2 = reconcile_health()
        assert report2.open_count == 1
        assert set(KnownIssue.objects.open().values_list("fingerprint", flat=True)) == {"sig-b"}

    def test_reconcile_failure_falls_open_to_read(self) -> None:
        KnownIssue.objects.record_signal(HealthSignal("live", KnownIssue.Severity.WARNING, "s"))
        with patch("teatree.core.operational_health.collect_signals", side_effect=RuntimeError("boom")):
            report = reconcile_health()
        # The pre-existing open row survives; the crash never resolves it.
        assert report.open_count == 1


class TestStaleTickCollector:
    def test_overrun_lease_yields_warning(self) -> None:
        LoopLease = apps.get_model("core", "LoopLease")
        now = timezone.now()
        LoopLease.objects.create(
            name="loop-wedged",
            acquired_at=now - timedelta(hours=3),
            lease_expires_at=now + timedelta(minutes=5),
        )
        with patch("teatree.config.cadence_seconds", return_value=60):
            signals = _stale_tick_signals()
        assert [s.fingerprint for s in signals] == ["stale-tick:loop-wedged"]
        assert signals[0].severity == KnownIssue.Severity.WARNING

    def test_fresh_lease_yields_nothing(self) -> None:
        LoopLease = apps.get_model("core", "LoopLease")
        now = timezone.now()
        LoopLease.objects.create(
            name="loop-fresh",
            acquired_at=now - timedelta(seconds=10),
            lease_expires_at=now + timedelta(minutes=5),
        )
        with patch("teatree.config.cadence_seconds", return_value=60):
            assert _stale_tick_signals() == []


class TestOverlaySignalCollector:
    def test_folds_every_overlay_signal_fail_open(self) -> None:
        class _Good:
            def get_health_signals(self) -> list[HealthSignal]:
                return [HealthSignal("ov:x", KnownIssue.Severity.WARNING, "overlay problem", overlay="acme")]

        class _Broken:
            def get_health_signals(self) -> list[HealthSignal]:
                raise RuntimeError(_OVERLAY_ON_FIRE)

        with patch(
            "teatree.core.operational_health.get_all_overlays",
            return_value={"acme": _Good(), "broken": _Broken()},
        ):
            signals = _overlay_health_signals()
        assert [s.fingerprint for s in signals] == ["ov:x"]
