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
from django.db import OperationalError
from django.utils import timezone

from teatree.core.admission_governor import MachineSignal, QuotaSignal
from teatree.core.factory import operational_health
from teatree.core.factory.operational_health import (
    HealthSignal,
    HealthStatus,
    SignalCollection,
    _admission_pressure_signals,
    _failed_task_signals,
    _fleet_loop_policy_signals,
    _harness_provider_consistency_signals,
    _overlay_health_signals,
    _stale_tick_signals,
    _status_from_issues,
    collect_signals,
    read_health,
    reconcile_health,
)
from teatree.core.models import ConfigSetting, Session, Task, Ticket
from teatree.core.models.config_setting import GLOBAL_SCOPE
from teatree.core.models.known_issue import KnownIssue
from teatree.utils.throttled_log import reset_throttle

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_OVERLAY_ON_FIRE = "overlay is on fire"
_DB_LOCKED = "database is locked"
_WEEK = 7 * 24 * 3600
_GOVERNOR = "teatree.core.admission_governor"


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


class TestAdmissionPressureClustersByCauseNotVolume:
    """#4508 — "telemetry volume is not incident volume".

    A saturated factory refuses on every admission decision, which on a busy box is
    hundreds of identical observations an hour. They are ONE incident, and the registry
    already knows how to say so: the fingerprint names the dominant CAUSE, so repeat
    sightings refresh one row instead of piling up.
    """

    def _collect(self, *, weekly: float = 0.1, load1: float = 1.0) -> SignalCollection:
        quota = QuotaSignal(
            fresh=True,
            all_accounts_exhausted=False,
            weekly_utilization=weekly,
            short_utilization=0.1,
            seconds_to_weekly_reset=_WEEK * 0.02,
        )
        machine = MachineSignal(cores=8, load1=load1, ram_available_gb=20.0)
        with (
            patch(f"{_GOVERNOR}.read_quota_signal", return_value=quota),
            patch(f"{_GOVERNOR}.read_machine_signal", return_value=machine),
        ):
            return _admission_pressure_signals()

    def test_a_healthy_factory_emits_nothing(self) -> None:
        assert self._collect().signals == ()

    def test_the_shed_band_is_a_warning_naming_its_cause(self) -> None:
        (signal,) = self._collect(weekly=0.92).signals
        assert signal.fingerprint == "admission-pressure:weekly-quota"
        assert signal.severity == KnownIssue.Severity.WARNING

    def test_the_halt_band_is_critical(self) -> None:
        (signal,) = self._collect(weekly=1.0).signals
        assert signal.severity == KnownIssue.Severity.CRITICAL

    def test_many_observations_of_one_cause_are_one_row(self) -> None:
        for _ in range(50):
            with patch.object(operational_health, "collect_signals", return_value=self._collect(weekly=1.0)):
                reconcile_health()
        rows = KnownIssue.objects.open().filter(kind="admission_pressure")
        assert rows.count() == 1
        assert rows.get().first_seen < rows.get().last_seen

    def test_the_row_auto_resolves_when_the_pressure_falls(self) -> None:
        with patch.object(operational_health, "collect_signals", return_value=self._collect(weekly=1.0)):
            reconcile_health()
        with patch.object(operational_health, "collect_signals", return_value=self._collect()):
            reconcile_health()
        assert not KnownIssue.objects.open().filter(kind="admission_pressure").exists()

    def test_an_unreadable_probe_names_itself_unread_and_resolves_nothing(self) -> None:
        with patch(f"{_GOVERNOR}.read_quota_signal", side_effect=OperationalError(_DB_LOCKED)):
            collection = _admission_pressure_signals()
        assert collection.signals == ()
        assert collection.unread == ("_admission_pressure_signals",)


class TestReconcileHealth:
    def test_reconcile_persists_and_resolves(self) -> None:
        signals = SignalCollection(
            (
                HealthSignal("sig-a", KnownIssue.Severity.WARNING, "a"),
                HealthSignal("sig-b", KnownIssue.Severity.CRITICAL, "b"),
            )
        )
        with patch("teatree.core.factory.operational_health.collect_signals", return_value=signals):
            report = reconcile_health()
        assert report.status is HealthStatus.RED
        assert report.open_count == 2
        # Next reconcile with sig-a gone auto-resolves it, leaves the critical.
        without_a = SignalCollection(signals.signals[1:])
        with patch("teatree.core.factory.operational_health.collect_signals", return_value=without_a):
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
            signals = _stale_tick_signals().signals
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
            assert _stale_tick_signals().signals == ()


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
            assert _stale_tick_signals().signals == ()

    def test_exclusion_is_targeted_a_wedged_work_loop_still_signals(self) -> None:
        # Same aged window across three leases: the ownership token and the
        # transient per-loop mutex are excluded, but a genuinely-overdue WORK
        # loop DOES still signal — the exclusion is targeted, not a blanket
        # neutering of the detector.
        self._lease("t3-master", acquired_ago=timedelta(minutes=25), owner_pid=os.getpid(), session_id="sess")
        self._lease("loop-tick:dispatch", acquired_ago=timedelta(minutes=25))
        self._lease("loop-tick", acquired_ago=timedelta(hours=3))
        with patch("teatree.config.cadence_seconds", return_value=60):
            fingerprints = {s.fingerprint for s in _stale_tick_signals().signals}
        assert fingerprints == {"stale-tick:loop-tick"}

    def test_reactive_lease_judged_against_its_own_cadence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A self-improve lease aged 25min is stale against the 720s tick cadence
        # (2x = 1440s) but fresh against its own 1800s cadence (2x = 3600s).
        monkeypatch.setenv("T3_SELF_IMPROVE_CHEAP_CADENCE", "1800")
        self._lease("loop-self-improve", acquired_ago=timedelta(minutes=25))
        with patch("teatree.config.cadence_seconds", return_value=720):
            assert _stale_tick_signals().signals == ()


class TestFailedTaskCollector:
    def _ticket_session(self, issue_url: str) -> tuple[Ticket, Session]:
        ticket = Ticket.objects.create(issue_url=issue_url, state=Ticket.State.STARTED)
        return ticket, Session.objects.create(overlay="test", ticket=ticket)

    def test_failed_task_in_window_yields_signal(self) -> None:
        ticket, session = self._ticket_session("https://example.com/issues/1")
        Task.objects.create(ticket=ticket, session=session, status=Task.Status.FAILED)
        signals = _failed_task_signals().signals
        assert [s.fingerprint for s in signals] == ["failed-tasks"]
        assert signals[0].severity == KnownIssue.Severity.WARNING

    def test_non_failed_task_yields_nothing(self) -> None:
        ticket, session = self._ticket_session("https://example.com/issues/2")
        Task.objects.create(ticket=ticket, session=session, status=Task.Status.COMPLETED)
        assert _failed_task_signals().signals == ()


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
            signals = _overlay_health_signals().signals
        assert [s.fingerprint for s in signals] == ["ov:x"]

    def test_broken_overlay_surfaces_a_warning_not_a_silent_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        # 3e#3: a persistently-failing overlay health read must redden into the log
        # (throttled warning), not be swallowed at debug where the chip silently
        # blanks on a real recurring fault.
        reset_throttle()

        class _Broken:
            def get_health_signals(self) -> list[HealthSignal]:
                raise RuntimeError(_OVERLAY_ON_FIRE)

        logger_name = "teatree.core.factory.operational_health"
        with (
            patch("teatree.core.factory.operational_health.get_all_overlays", return_value={"broken": _Broken()}),
            caplog.at_level("DEBUG", logger=logger_name),
        ):
            _overlay_health_signals()
        warnings = [r for r in caplog.records if r.name == logger_name and r.levelname == "WARNING"]
        assert warnings, "expected a throttled WARNING for the broken overlay health read"
        assert "broken" in warnings[0].getMessage()


class TestHarnessProviderConsistencyCollector:
    """The loop-admission / health guard for the coupled harness/provider pair (#3688).

    A pair set before the write-time guard existed (or via an uncovered path)
    surfaces as ONE loud CRITICAL health-red, not a per-task repair-halt flood.
    Rows are created directly via the ORM to model that pre-existing state — the
    write-time guard would refuse the inconsistent ``set_value``.
    """

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)
        monkeypatch.delenv("T3_AGENT_HARNESS_PROVIDER", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_consistent_effective_pair_yields_nothing(self) -> None:
        with patch("teatree.core.factory.operational_health.get_all_overlays", return_value={}):
            assert _harness_provider_consistency_signals().signals == ()

    def test_preexisting_inconsistent_pair_yields_a_critical_signal(self) -> None:
        ConfigSetting.objects.create(scope=GLOBAL_SCOPE, key="agent_harness_provider", value="openai_compatible")
        with patch("teatree.core.factory.operational_health.get_all_overlays", return_value={}):
            signals = _harness_provider_consistency_signals().signals
        assert len(signals) == 1
        assert signals[0].severity == KnownIssue.Severity.CRITICAL
        assert signals[0].kind == "config_pair_drift"

    def test_inconsistent_pair_reddens_the_chip_via_reconcile(self) -> None:
        ConfigSetting.objects.create(scope=GLOBAL_SCOPE, key="agent_harness_provider", value="openai_compatible")
        with patch("teatree.core.factory.operational_health.get_all_overlays", return_value={}):
            report = reconcile_health()
        assert report.status is HealthStatus.RED


class TestFleetLoopPolicySignals:
    """An unsatisfiable fleet loop declaration is a durable signal, not deploy stderr.

    ``deploy/entrypoint.sh`` warns and continues (crash-looping init on the config the
    box already shipped would be worse than the mis-mask), but that warning lives only
    in the deploy log. The collector re-derives the same verdict from the env compose
    hands every service, so the chip stays yellow until the repo variable is fixed.
    """

    def test_contradictory_declaration_yields_one_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEATREE_ENABLED_LOOPS", raising=False)
        monkeypatch.setenv("TEATREE_DISABLED_LOOPS", "inbox,directive_loop")
        signals = _fleet_loop_policy_signals().signals
        assert len(signals) == 1
        assert signals[0].severity == KnownIssue.Severity.WARNING
        assert signals[0].fingerprint == "fleet-loop-policy-contradiction"
        assert "review" in signals[0].summary

    def test_sound_declaration_yields_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEATREE_ENABLED_LOOPS", "inbox")
        monkeypatch.setenv("TEATREE_DISABLED_LOOPS", "review")
        assert _fleet_loop_policy_signals().signals == ()

    def test_unset_declaration_yields_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEATREE_ENABLED_LOOPS", raising=False)
        monkeypatch.delenv("TEATREE_DISABLED_LOOPS", raising=False)
        assert _fleet_loop_policy_signals().signals == ()

    def test_contradiction_yellows_the_chip_via_reconcile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEATREE_ENABLED_LOOPS", raising=False)
        monkeypatch.setenv("TEATREE_DISABLED_LOOPS", "inbox,directive_loop")
        with patch("teatree.core.factory.operational_health.get_all_overlays", return_value={}):
            report = reconcile_health()
        assert report.status is HealthStatus.YELLOW
        assert any(issue.fingerprint == "fleet-loop-policy-contradiction" for issue in report.open_issues)


class _BrokenOverlay:
    def get_health_signals(self) -> list[HealthSignal]:
        raise RuntimeError(_OVERLAY_ON_FIRE)


class TestUnreadCollectorNeverAutoResolves:
    """A collector that could not READ must not retire the issue it can no longer see (#4354).

    An unread collector and a collector reporting "all clear" produced the identical
    empty slice, so ``reconcile`` read absence as RESOLUTION: a CRITICAL retired itself
    the first tick its own reader hiccupped, and the chip went green on an unchanged
    fault.
    """

    def test_raising_collector_neither_resolves_the_row_nor_greens_the_chip(self) -> None:
        KnownIssue.objects.record_signal(HealthSignal("failed-tasks", KnownIssue.Severity.CRITICAL, "3 failed"))

        def _cannot_read() -> SignalCollection:
            raise OperationalError(_DB_LOCKED)

        with patch.object(operational_health, "_COLLECTORS", (_cannot_read,)):
            report = reconcile_health()

        open_fingerprints = set(KnownIssue.objects.open().values_list("fingerprint", flat=True))
        assert "failed-tasks" in open_fingerprints
        assert "health-collector-failed:_cannot_read" in open_fingerprints
        assert report.status is HealthStatus.RED

    def test_broken_overlay_does_not_retire_the_row_the_working_collectors_kept(self) -> None:
        KnownIssue.objects.record_signal(HealthSignal("stale-tick:loop-a", KnownIssue.Severity.WARNING, "wedged"))

        with (
            patch.object(operational_health, "_COLLECTORS", (_overlay_health_signals,)),
            patch(
                "teatree.core.factory.operational_health.get_all_overlays",
                return_value={"broken": _BrokenOverlay()},
            ),
        ):
            report = reconcile_health()

        open_fingerprints = set(KnownIssue.objects.open().values_list("fingerprint", flat=True))
        assert "stale-tick:loop-a" in open_fingerprints
        assert "health-collector-failed:overlay:broken" in open_fingerprints
        assert report.status is HealthStatus.RED

    def test_a_complete_observation_still_auto_resolves(self) -> None:
        """The control: when every collector ANSWERED, a cleared signal still retires."""
        KnownIssue.objects.record_signal(HealthSignal("gone", KnownIssue.Severity.WARNING, "cleared"))

        def _all_clear() -> SignalCollection:
            return SignalCollection()

        with patch.object(operational_health, "_COLLECTORS", (_all_clear,)):
            report = reconcile_health()

        assert KnownIssue.objects.open().count() == 0
        assert report.status is HealthStatus.GREEN

    def test_collect_signals_names_the_collector_that_could_not_read(self) -> None:
        def _cannot_read() -> SignalCollection:
            raise OperationalError(_DB_LOCKED)

        with patch.object(operational_health, "_COLLECTORS", (_cannot_read,)):
            collection = collect_signals()

        assert collection.unread == ("_cannot_read",)
        assert collection.complete is False
