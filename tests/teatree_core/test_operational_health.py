"""The global operational-health aggregator (PR-17, M6).

Distinct from ``teatree.core.worktree.health`` (per-worktree readiness). This module
computes the green/yellow/red factory-health verdict from deterministic durable
signals and persists them as ``KnownIssue`` rows.
"""

import os
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.apps import apps
from django.utils import timezone

from teatree.core.factory.operational_health import (
    HealthSignal,
    HealthStatus,
    _failed_task_signals,
    _overlay_health_signals,
    _stale_tick_signals,
    _status_from_issues,
    read_health,
    reconcile_health,
)
from teatree.core.models import Session, Task, Ticket
from teatree.core.models.known_issue import KnownIssue

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
        with patch("teatree.core.factory.operational_health.collect_signals", return_value=signals):
            report = reconcile_health()
        assert report.status is HealthStatus.RED
        assert report.open_count == 2
        # Next reconcile with sig-a gone auto-resolves it, leaves the critical.
        with patch("teatree.core.factory.operational_health.collect_signals", return_value=signals[1:]):
            report2 = reconcile_health()
        assert report2.open_count == 1
        assert set(KnownIssue.objects.open().values_list("fingerprint", flat=True)) == {"sig-b"}

    def test_reconcile_failure_falls_open_to_read(self) -> None:
        KnownIssue.objects.record_signal(HealthSignal("live", KnownIssue.Severity.WARNING, "s"))
        with patch("teatree.core.factory.operational_health.collect_signals", side_effect=RuntimeError("boom")):
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


class TestStaleTickExclusions:
    """Ownership tokens + transient mutexes excluded; each lease judged against its OWN cadence/TTL."""

    def _lease(self, name: str, *, acquired_ago: timedelta, **kw: object) -> None:
        lease_model = apps.get_model("core", "LoopLease")
        now = timezone.now()
        lease_model.objects.create(
            name=name,
            acquired_at=now - acquired_ago,
            lease_expires_at=now + timedelta(minutes=5),
            **kw,
        )

    def test_busy_t3_master_within_ttl_is_not_stale(self) -> None:
        # A busy t3-master: acquired_at aged past the tick cutoff but still within
        # its 1800s owner TTL (lease live) with the owner process alive. It is a
        # pid-anchored ownership token (busy != dead, #1073/#1604), never a wedged
        # tick — flagging it would redden the health chip on a healthy factory.
        self._lease("t3-master", acquired_ago=timedelta(minutes=25), owner_pid=os.getpid(), session_id="sess-busy")
        with patch("teatree.config.cadence_seconds", return_value=60):
            assert _stale_tick_signals() == []

    def test_exclusion_is_targeted_a_wedged_work_loop_still_signals(self) -> None:
        # Same aged window across three leases: the ownership token and the
        # transient per-loop mutex are excluded, but a genuinely-overdue WORK
        # loop DOES still signal — the exclusion is targeted, not a blanket
        # neutering of the detector.
        self._lease("t3-master", acquired_ago=timedelta(minutes=25), owner_pid=os.getpid(), session_id="sess")
        self._lease("loop-tick:dispatch", acquired_ago=timedelta(minutes=25))
        self._lease("loop-tick", acquired_ago=timedelta(hours=3))
        with patch("teatree.config.cadence_seconds", return_value=60):
            fingerprints = {s.fingerprint for s in _stale_tick_signals()}
        assert fingerprints == {"stale-tick:loop-tick"}

    def test_reactive_lease_judged_against_its_own_cadence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A self-improve lease aged 25min is stale against the 720s tick cadence
        # (2x = 1440s) but fresh against its own 1800s cadence (2x = 3600s).
        monkeypatch.setenv("T3_SELF_IMPROVE_CHEAP_CADENCE", "1800")
        self._lease("loop-self-improve", acquired_ago=timedelta(minutes=25))
        with patch("teatree.config.cadence_seconds", return_value=720):
            assert _stale_tick_signals() == []


class TestFailedTaskCollector:
    def _ticket_session(self, issue_url: str) -> tuple[Ticket, Session]:
        ticket = Ticket.objects.create(issue_url=issue_url, state=Ticket.State.STARTED)
        return ticket, Session.objects.create(overlay="test", ticket=ticket)

    def test_failed_task_in_window_yields_signal(self) -> None:
        ticket, session = self._ticket_session("https://example.com/issues/1")
        Task.objects.create(ticket=ticket, session=session, status=Task.Status.FAILED)
        signals = _failed_task_signals()
        assert [s.fingerprint for s in signals] == ["failed-tasks"]
        assert signals[0].severity == KnownIssue.Severity.WARNING

    def test_non_failed_task_yields_nothing(self) -> None:
        ticket, session = self._ticket_session("https://example.com/issues/2")
        Task.objects.create(ticket=ticket, session=session, status=Task.Status.COMPLETED)
        assert _failed_task_signals() == []


class TestOverlaySignalCollector:
    def test_folds_every_overlay_signal_fail_open(self) -> None:
        class _Good:
            def get_health_signals(self) -> list[HealthSignal]:
                return [HealthSignal("ov:x", KnownIssue.Severity.WARNING, "overlay problem", overlay="acme")]

        class _Broken:
            def get_health_signals(self) -> list[HealthSignal]:
                raise RuntimeError(_OVERLAY_ON_FIRE)

        with patch(
            "teatree.core.factory.operational_health.get_all_overlays",
            return_value={"acme": _Good(), "broken": _Broken()},
        ):
            signals = _overlay_health_signals()
        assert [s.fingerprint for s in signals] == ["ov:x"]
