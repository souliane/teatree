"""``manage.py loops_tick`` — the single PER-LOOP tick surface (#2650).

Drives the real command via ``call_command`` with the dispatch pipeline and
backends mocked. There is NO master tick: bare ``t3 loops tick`` (no ``--loop``)
is a hard error, and the per-loop tick claims the disjoint ``loop:<name>`` owner
lease + ``loop-tick:<name>`` mutex, re-anchors a deferred reinstall behind the
``loop-reinstall`` lease, installs the statusline schedules reader, and runs
``run_tick`` scoped to that one loop — never a reactive piggyback cycle.
"""

import datetime as dt
import io
import os
from contextlib import nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock, patch

import django.test
import pytest
from django.core.management import call_command
from django.utils import timezone

from teatree.core.admission_pressure import MachineSignal, QuotaSignal
from teatree.core.agent_admission import AgentAdmission, agent_admission_verdict
from teatree.core.management.commands.loops_tick import Command, _ScopedJobsBuilder
from teatree.core.models import (
    ConfigSetting,
    KnownIssue,
    Loop,
    LoopLease,
    Prompt,
    PullRequest,
    Session,
    Task,
    Ticket,
    Worktree,
)
from teatree.core.overlay import OverlayBase, OverlayConnectors, ProvisionStep
from teatree.loop.domain_jobs import _run_job
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.phases.scan import ScanOutcome
from teatree.loop.scanners.architectural_review import ArchitecturalReviewScanner
from teatree.loop.scanners.backlog_sweep import BacklogSweepScanner
from teatree.loop.scanners.base import Scanner, ScanSignal
from teatree.loop.scanners.eval_local import EvalLocalScanner
from teatree.loop.scanners.phase_cadence import PhaseCadence
from teatree.loop.scanners.provision_smoke import ProvisionSmokeScanner
from teatree.loop.scanners.scanning_news import ScanningNewsScanner
from teatree.loop.scanners.triage_assessor import TriageAssessorScanner
from teatree.loop.tick import TickReport
from teatree.loops.base import MiniLoop
from teatree.loops.deadlined_tick import TICK_SUBPROCESS_ENV_MARKER
from teatree.loops.registry import iter_loops
from teatree.token_report import EvalReserveDecision

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend


class TestHardExitGuard:
    """The post-render ``os._exit`` fires ONLY in the worker's deadlined subprocess (#7).

    A hung non-daemon scanner thread blocks interpreter shutdown, pinning the subprocess
    (and a scarce ``loops`` executor slot); the hard exit reclaims it right after render.
    An in-process ``call_command`` (tests) must NEVER hit it — gated on the env marker
    ``run_deadlined_tick`` sets only on the spawned subprocess.
    """

    def test_no_hard_exit_without_the_subprocess_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []
        monkeypatch.setattr(os, "_exit", calls.append)
        monkeypatch.delenv(TICK_SUBPROCESS_ENV_MARKER, raising=False)

        Command._hard_exit_if_subprocess()

        assert calls == []  # an in-process invocation returns normally, never os._exit

    def test_hard_exit_when_the_subprocess_marker_is_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []
        monkeypatch.setattr(os, "_exit", calls.append)
        monkeypatch.setenv(TICK_SUBPROCESS_ENV_MARKER, "1")

        Command._hard_exit_if_subprocess()

        assert calls == [0]  # the deadlined subprocess exits immediately after render


class TestHeldCadenceRelease:
    def test_diagnostic_signals_leave_the_tick_idle(self) -> None:
        command = Command()
        for kind in ("architectural_review.not_due", "triage_assessor.held", "eval_local.held"):
            report = TickReport(
                started_at=dt.datetime.now(dt.UTC),
                signals=[ScanSignal(kind=kind, summary="nothing to run")],
            )
            assert command._emit_report(report, "arch_review", json_output=False, blocked_reason="") is False

    def test_queued_review_counts_as_work(self) -> None:
        report = TickReport(
            started_at=dt.datetime.now(dt.UTC),
            signals=[ScanSignal(kind="architectural_review.queued", summary="review queued")],
        )
        assert Command()._emit_report(report, "arch_review", json_output=False, blocked_reason="") is True

    @pytest.mark.parametrize(
        "kind",
        [
            "triage_assessor.queued",
            "backlog_sweep.queued",
            "scanning_news.queued",
            "dogfood_smoke.queued",
            "review_done_ack.queued",
        ],
    )
    def test_queued_statusline_dropped_signal_counts_as_work(self, kind: str) -> None:
        report = TickReport(
            started_at=dt.datetime.now(dt.UTC),
            signals=[ScanSignal(kind=kind, summary="task queued")],
        )
        assert Command()._emit_report(report, "arch_review", json_output=False, blocked_reason="") is True

    def test_architectural_review_hold_releases_the_claimed_anchor(self) -> None:
        claimed = dt.datetime.now(dt.UTC)
        builder = _ScopedJobsBuilder(loop="arch_review", claimed_at=claimed)
        report = TickReport(
            started_at=claimed,
            signals=[ScanSignal(kind="architectural_review.held", summary="review lane held", payload={"retry": True})],
        )

        with patch.object(Loop.objects, "release_held_run_if_unchanged") as release:
            builder.release_held_cadence(report)

        release.assert_called_once_with("arch_review", claimed_at=claimed, previous_last_run_at=None)

    def test_only_retryable_hold_releases_the_claimed_anchor(self) -> None:
        claimed = dt.datetime.now(dt.UTC)
        builder = _ScopedJobsBuilder(loop="triage_assessor", claimed_at=claimed)
        with patch.object(Loop.objects, "release_held_run_if_unchanged") as release:
            builder.release_held_cadence(
                TickReport(
                    started_at=claimed,
                    signals=[ScanSignal(kind="triage_assessor.held", summary="empty")],
                )
            )
            release.assert_not_called()
            eval_builder = _ScopedJobsBuilder(loop="eval_local", claimed_at=claimed)
            eval_builder.release_held_cadence(
                TickReport(
                    started_at=claimed,
                    signals=[ScanSignal(kind="eval_local.held", summary="reserve held", payload={"retry": True})],
                )
            )
            release.assert_called_once_with("eval_local", claimed_at=claimed, previous_last_run_at=None)


def _run(**kwargs: object) -> str:
    # emit() sends JSON to stdout and the human view to stderr — one channel per
    # call — so their concatenation is the single populated output stream.
    out = io.StringIO()
    err = io.StringIO()
    call_command("loops_tick", stdout=out, stderr=err, **kwargs)
    return out.getvalue() + err.getvalue()


class _CleanOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        _ = worktree
        return []


class _SlackDownOverlayConnectors(OverlayConnectors):
    def preflight(self) -> list:
        def _probe() -> None:
            msg = "Slack auth.test failed: missing_scope"
            raise RuntimeError(msg)

        return [_probe]


class _SlackDownOverlay(OverlayBase):
    connectors = _SlackDownOverlayConnectors()

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        _ = worktree
        return []


class TestBareTickRefused(django.test.TestCase):
    """``t3 loops tick`` with no ``--loop`` is a hard error — there is no master tick (#2650)."""

    def test_bare_tick_exits_nonzero_and_claims_no_lease(self) -> None:
        err = io.StringIO()
        with (
            patch.object(LoopLease.objects, "claim_ownership") as claim,
            patch("teatree.loop.tick.run_tick") as run_tick,
            pytest.raises(SystemExit) as exc,
        ):
            call_command("loops_tick", stderr=err)
        assert exc.value.code == 2
        # No fan-out, no lease claim — the bare path never reaches the tick body.
        claim.assert_not_called()
        run_tick.assert_not_called()

    def test_bare_tick_message_points_at_per_loop_usage(self) -> None:
        err = io.StringIO()
        with pytest.raises(SystemExit):
            call_command("loops_tick", stderr=err)
        message = err.getvalue()
        assert "--loop" in message
        assert "no master tick" in message.lower()

    def test_blank_loop_flag_is_also_refused(self) -> None:
        err = io.StringIO()
        with pytest.raises(SystemExit) as exc:
            call_command("loops_tick", loop="   ", stderr=err)
        assert exc.value.code == 2


class TestLoopsTickPerLoop(django.test.TestCase):
    """``t3 loops tick --loop <name>`` — one enabled DB Loop per native ``/loop`` (#2650)."""

    def test_loop_flag_claims_the_per_loop_owner_lease(self) -> None:
        report = TickReport(started_at=dt.datetime.now(dt.UTC))
        captured: dict[str, str] = {}

        def _claim(slot: str, **_: object) -> tuple[bool, str]:
            captured["slot"] = slot
            return (True, "me")

        with (
            patch.object(LoopLease.objects, "claim_ownership", side_effect=_claim),
            patch.object(LoopLease.objects, "acquire", return_value=True),
            patch.object(LoopLease.objects, "release"),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            _run(loop="inbox")
        # A disjoint per-loop owner key (``loop:<name>``), never a singleton owner
        # — so the N per-loop ``/loop``s run in parallel, not serialised.
        assert captured["slot"] == "loop:inbox"

    def test_loop_flag_acquires_the_per_loop_tick_mutex(self) -> None:
        report = TickReport(started_at=dt.datetime.now(dt.UTC))
        acquired: list[str] = []

        def _acquire(slot: str, **_: object) -> bool:
            acquired.append(slot)
            return True

        with (
            patch.object(LoopLease.objects, "claim_ownership", return_value=(True, "me")),
            patch.object(LoopLease.objects, "acquire", side_effect=_acquire),
            patch.object(LoopLease.objects, "release"),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            _run(loop="inbox")
        # The per-loop tick mutex is ``loop-tick:<name>`` (never the bare master
        # ``loop-tick``), so ticks of the same loop serialise but distinct loops do not.
        assert "loop-tick:inbox" in acquired

    def test_per_loop_skip_names_the_real_per_loop_slot(self) -> None:
        with (
            patch.object(LoopLease.objects, "claim_ownership", return_value=(False, "other-session")),
            patch("teatree.loop.tick.run_tick") as run_tick,
        ):
            out = _run(loop="dispatch")
        assert "t3 loop claim --slot loop:dispatch --take-over" in out
        run_tick.assert_not_called()

    def test_loop_flag_scopes_the_jobs_builder_to_that_one_loop(self) -> None:
        report = TickReport(started_at=dt.datetime.now(dt.UTC))
        with (
            patch.object(LoopLease.objects, "claim_ownership", return_value=(True, "me")),
            patch.object(LoopLease.objects, "acquire", return_value=True),
            patch.object(LoopLease.objects, "release"),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report) as run_tick,
        ):
            _run(loop="inbox")
        from teatree.loop.tick import TickRequest  # noqa: PLC0415

        jobs_builder = run_tick.call_args.kwargs["jobs_builder"]
        # The builder consumes the REASONED fan-out (#3843) so it can report why a
        # refused loop produced no jobs; the `only` scoping it pins is unchanged.
        with patch("teatree.loops.loop_table.dispatch_loop_table", return_value=[]) as build:
            jobs_builder(TickRequest(), dt.datetime.now(dt.UTC))
        assert build.call_args.kwargs["only"] == "inbox"

    def test_held_scanner_rechecks_on_the_next_tick(self) -> None:
        class _Scanner:
            name = "eval_local"
            scans = 0

            def scan(self) -> list[ScanSignal]:
                self.scans += 1
                kind = "eval_local.held" if self.scans == 1 else "eval_local.queued"
                return [ScanSignal(kind=kind, summary=kind, payload={"retry": self.scans == 1})]

        scanner = _Scanner()
        prompt = Prompt.objects.create(name="eval-local-retry-prompt", body="test")
        Loop.objects.get_or_create(name="eval_local", defaults={"delay_seconds": 3600, "prompt": prompt})
        Loop.objects.filter(name="eval_local").update(delay_seconds=3600, last_run_at=None)
        mini = MiniLoop(
            name="eval_local",
            default_cadence_seconds=3600,
            build_jobs=lambda **_: [_ScannerJob(scanner=scanner, overlay="")],
        )
        with (
            patch.object(LoopLease.objects, "claim_ownership", return_value=(True, "me")),
            patch.object(LoopLease.objects, "acquire", return_value=True),
            patch.object(LoopLease.objects, "release"),
            patch("teatree.loops.connector_preflight.run_loop_connector_preflight"),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loops.loop_table.iter_loops", return_value=(mini,)),
            patch("teatree.loop.tick.render_phase"),
        ):
            _run(loop="eval_local")
            assert Loop.objects.get(name="eval_local").last_run_at is None
            _run(loop="eval_local")

        assert scanner.scans == 2
        assert Loop.objects.get(name="eval_local").last_run_at is not None

    def test_empty_triage_keeps_anchor_until_next_cadence(self) -> None:
        prompt = Prompt.objects.create(name="triage-cadence-prompt", body="test")
        for repo_slugs in (("souliane/teatree",), ()):
            with self.subTest(repo_slugs=repo_slugs):
                host = Mock()
                host.list_labeled_issues.return_value = []
                scanner = TriageAssessorScanner(
                    host=cast("CodeHostBackend", host),
                    overlay_name="t3-teatree",
                    identities=("operator",),
                    repo_slugs=repo_slugs,
                )
                Loop.objects.get_or_create(name="triage_assessor", defaults={"delay_seconds": 3600, "prompt": prompt})
                Loop.objects.filter(name="triage_assessor").update(delay_seconds=3600, last_run_at=None)
                mini = MiniLoop(
                    name="triage_assessor",
                    default_cadence_seconds=3600,
                    build_jobs=lambda _scanner=scanner, **_: [_ScannerJob(scanner=_scanner, overlay="")],
                    periodic_phase="triage_assessing",
                )
                with (
                    patch.object(LoopLease.objects, "claim_ownership", return_value=(True, "me")),
                    patch.object(LoopLease.objects, "acquire", return_value=True),
                    patch.object(LoopLease.objects, "release"),
                    patch("teatree.loops.connector_preflight.run_loop_connector_preflight"),
                    patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
                    patch("teatree.loops.loop_table.iter_loops", return_value=(mini,)),
                    patch("teatree.loop.tick.render_phase"),
                    patch("teatree.loop.tick.scan_phase", side_effect=TestPeriodicLoopHandover._scan_inline),
                ):
                    _run(loop="triage_assessor")
                    claimed_at = Loop.objects.get(name="triage_assessor").last_run_at
                    _run(loop="triage_assessor")

                assert claimed_at is not None
                assert Loop.objects.get(name="triage_assessor").last_run_at == claimed_at
                assert host.list_labeled_issues.call_count == (1 if repo_slugs else 0)

    def test_named_non_retry_returns_keep_all_periodic_anchors(self) -> None:
        phases = {
            mini.name: mini.periodic_phase
            for mini in iter_loops()
            if mini.name in {"arch_review", "eval_local", "news", "backlog_sweep", "dogfood", "triage_assessor"}
        }
        assert len(phases) == 6

        for reason in ("task already in flight", "cadence has not elapsed"):
            for loop, phase in phases.items():
                with self.subTest(loop=loop, reason=reason):
                    overlay = f"anchor-{loop}-{reason.replace(' ', '-')}"
                    ticket = Ticket.objects.create(overlay=overlay)
                    session = Session.objects.create(overlay=overlay, ticket=ticket)
                    status = Task.Status.CLAIMED if reason == "task already in flight" else Task.Status.COMPLETED
                    task = Task.objects.create(ticket=ticket, session=session, phase=phase, status=status)
                    if status == Task.Status.CLAIMED:
                        Task.objects.filter(pk=task.pk).update(admitted_at=timezone.now())

                    class _NamedReturnScanner:
                        def __init__(self, *, overlay: str, phase: str, reason: str) -> None:
                            self.name = phase
                            self.overlay = overlay
                            self.reason = reason
                            self.scans = 0

                        def scan(self) -> list[ScanSignal]:
                            self.scans += 1
                            trigger, signals = PhaseCadence(self.overlay, phase=self.name, cadence_hours=24).gate(
                                now=timezone.now(), held_kind=f"{self.name}.held", not_due_kind=f"{self.name}.not_due"
                            )
                            assert trigger is None
                            assert signals[0].payload["reason"] == self.reason
                            return signals

                    scanner = _NamedReturnScanner(overlay=overlay, phase=phase, reason=reason)
                    prompt = Prompt.objects.create(name=f"not-due-{loop}-{reason}", body="test")
                    Loop.objects.update_or_create(
                        name=loop,
                        defaults={
                            "prompt": prompt,
                            "script": "",
                            "delay_seconds": 3600,
                            "daily_at": None,
                            "enabled": True,
                            "last_run_at": None,
                        },
                    )
                    mini = MiniLoop(
                        name=loop,
                        default_cadence_seconds=3600,
                        build_jobs=lambda _scanner=scanner, **_: [_ScannerJob(scanner=_scanner, overlay="")],
                        periodic_phase=phase,
                    )
                    with (
                        patch.object(LoopLease.objects, "claim_ownership", return_value=(True, "me")),
                        patch.object(LoopLease.objects, "acquire", return_value=True),
                        patch.object(LoopLease.objects, "release"),
                        patch("teatree.loops.connector_preflight.run_loop_connector_preflight"),
                        patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
                        patch("teatree.loops.loop_table.iter_loops", return_value=(mini,)),
                        patch("teatree.loop.tick.render_phase"),
                        patch("teatree.loop.tick.scan_phase", side_effect=TestPeriodicLoopHandover._scan_inline),
                    ):
                        _run(loop=loop)
                        claimed_at = Loop.objects.get(name=loop).last_run_at
                        _run(loop=loop)

                    assert claimed_at is not None
                    assert Loop.objects.get(name=loop).last_run_at == claimed_at
                    assert scanner.scans == 1

    def test_periodic_scanner_silence_records_defect_and_retries(self) -> None:
        class _SilentScanner:
            name = "scanning_news"

            def scan(self) -> list[ScanSignal]:
                return []

        prompt = Prompt.objects.create(name="silent-news-prompt", body="test")
        Loop.objects.get_or_create(name="news", defaults={"delay_seconds": 3600, "prompt": prompt})
        Loop.objects.filter(name="news").update(delay_seconds=3600, daily_at=None, last_run_at=None)
        Loop.objects.set_manual_override("news", runs=True, reason="exercise periodic tick")
        mini = MiniLoop(
            name="news",
            default_cadence_seconds=3600,
            build_jobs=lambda **_: [_ScannerJob(scanner=_SilentScanner(), overlay="")],
            periodic_phase="scanning_news",
        )
        with (
            patch.object(LoopLease.objects, "claim_ownership", return_value=(True, "me")),
            patch.object(LoopLease.objects, "acquire", return_value=True),
            patch.object(LoopLease.objects, "release"),
            patch("teatree.loops.connector_preflight.run_loop_connector_preflight"),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loops.loop_table.iter_loops", return_value=(mini,)),
            patch("teatree.loop.tick.render_phase"),
            self.assertLogs("teatree.loops.tick_outcome", level="WARNING") as logs,
        ):
            _run(loop="news")

        assert any("news" in line and "no signal" in line for line in logs.output)
        assert KnownIssue.objects.open().filter(fingerprint="loop-silent:news").exists()
        assert Loop.objects.get(name="news").last_run_at is None

    def test_periodic_factory_with_no_job_names_reason_and_keeps_anchor(self) -> None:
        prompt = Prompt.objects.create(name="no-job-news-prompt", body="test")
        Loop.objects.get_or_create(name="news", defaults={"delay_seconds": 3600, "prompt": prompt})
        Loop.objects.filter(name="news").update(delay_seconds=3600, daily_at=None, last_run_at=None)
        Loop.objects.set_manual_override("news", runs=True, reason="exercise periodic tick")
        build_jobs = Mock(return_value=[])
        mini = MiniLoop(
            name="news",
            default_cadence_seconds=3600,
            build_jobs=build_jobs,
            periodic_phase="scanning_news",
        )
        with (
            patch.object(LoopLease.objects, "claim_ownership", return_value=(True, "me")),
            patch.object(LoopLease.objects, "acquire", return_value=True),
            patch.object(LoopLease.objects, "release"),
            patch("teatree.loops.connector_preflight.run_loop_connector_preflight"),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loops.loop_table.iter_loops", return_value=(mini,)),
            patch("teatree.loop.tick.render_phase"),
            self.assertLogs("teatree.loops.tick_outcome", level="WARNING") as logs,
        ):
            _run(loop="news")
            claimed_at = Loop.objects.get(name="news").last_run_at
            _run(loop="news")

        assert any("news" in line and "no scanner job" in line for line in logs.output)
        assert KnownIssue.objects.open().filter(fingerprint="loop-silent:news").exists()
        assert claimed_at is not None
        assert Loop.objects.get(name="news").last_run_at == claimed_at
        build_jobs.assert_called_once()


def _periodic_scanner(loop: str) -> Scanner:
    overlay = "t3-teatree"
    if loop == "arch_review":
        return ArchitecturalReviewScanner(overlay_name=overlay)
    if loop == "eval_local":
        return EvalLocalScanner(overlay_name=overlay, reserve_met=lambda: EvalReserveDecision(met=True))
    if loop == "news":
        return ScanningNewsScanner(overlay_name=overlay)
    if loop == "backlog_sweep":
        return BacklogSweepScanner(overlay_name=overlay)
    if loop == "dogfood":
        return ProvisionSmokeScanner(overlay_name=overlay, skill="dogfood")
    host = Mock()
    host.list_assigned_issues.return_value = []
    host.list_labeled_issues.return_value = [
        {
            "html_url": "https://github.com/souliane/teatree/issues/700",
            "title": "Assess this issue",
            "state": "open",
            "labels": ["needs-triage"],
        }
    ]
    return TriageAssessorScanner(
        host=cast("CodeHostBackend", host),
        overlay_name=overlay,
        identities=("operator",),
        repo_slugs=("souliane/teatree",),
    )


class TestPeriodicLoopHandover(django.test.TransactionTestCase):
    @staticmethod
    def _scan_inline(jobs: list[_ScannerJob]) -> ScanOutcome:
        outcome = ScanOutcome()
        for job in jobs:
            label, signals, error = _run_job(job)
            outcome.signals.extend(signals)
            if error:
                outcome.errors[label] = error
        return outcome

    def test_all_periodic_units_admit_in_their_tick(self) -> None:
        self._exercise(blocked=False)

    def test_all_periodic_units_name_a_machine_hold_and_retry(self) -> None:
        self._exercise(blocked=True)

    def test_due_maintenance_admits_with_eight_coding_tasks_and_three_open_prs(self) -> None:
        ConfigSetting.objects.set_value("admission_write_concurrency_per_core", 1.0)
        ticket = Ticket.objects.create(overlay="t3-teatree")
        session = Session.objects.create(ticket=ticket, overlay="t3-teatree")
        Task.objects.bulk_create(
            Task(
                ticket=ticket,
                session=session,
                phase="coding",
                status=Task.Status.CLAIMED,
                lease_expires_at=timezone.now() + dt.timedelta(hours=1),
            )
            for _ in range(8)
        )
        PullRequest.objects.bulk_create(
            PullRequest(
                ticket=ticket,
                overlay="t3-teatree",
                url=f"https://github.com/souliane/teatree/pull/{number}",
                repo="souliane/teatree",
                iid=str(number),
            )
            for number in range(700, 703)
        )
        assert PullRequest.objects.filter(state=PullRequest.State.OPEN).count() == 3
        self._exercise(blocked=False, real_pressure=True)

    def _exercise(self, *, blocked: bool, real_pressure: bool = False) -> None:
        loops = (
            ("arch_review", "eval_local")
            if real_pressure
            else ("arch_review", "eval_local", "news", "backlog_sweep", "dogfood", "triage_assessor")
        )
        for loop in loops:
            with self.subTest(loop=loop):
                mini = next(item for item in iter_loops() if item.name == loop)
                phase = mini.periodic_phase
                scanner = _periodic_scanner(loop)
                mini = replace(
                    mini, build_jobs=lambda _scanner=scanner, **_: [_ScannerJob(scanner=_scanner, overlay="")]
                )
                prompt = Prompt.objects.create(name=f"periodic-{loop}", body="test")
                Loop.objects.update_or_create(
                    name=loop,
                    defaults={
                        "prompt": prompt,
                        "script": "",
                        "delay_seconds": 60,
                        "daily_at": None,
                        "enabled": True,
                        "last_run_at": None,
                    },
                )

                def admission() -> AgentAdmission:
                    return AgentAdmission(
                        expensive_denied=None,
                        review_denied=None,
                        retiring_denied=None,
                        cheap_denied=None,
                        architectural_review_denied="machine HALT" if blocked else None,
                        maintenance_denied="machine HALT" if blocked else None,
                    )

                with (
                    patch.object(LoopLease.objects, "claim_ownership", return_value=(True, "me")),
                    patch.object(LoopLease.objects, "acquire", return_value=True),
                    patch.object(LoopLease.objects, "release"),
                    patch.object(Ticket, "has_dispatchable_overlay", return_value=True),
                    patch("teatree.loops.connector_preflight.run_loop_connector_preflight"),
                    patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
                    patch("teatree.loops.loop_table.iter_loops", return_value=(mini,)),
                    patch("teatree.loop.tick.render_phase"),
                    patch("teatree.loop.tick.scan_phase", side_effect=self._scan_inline),
                    (
                        nullcontext()
                        if real_pressure
                        else patch("teatree.core.agent_admission.agent_admission_verdict", side_effect=admission)
                    ),
                    (
                        patch(
                            "teatree.core.agent_admission.read_quota_signal",
                            return_value=QuotaSignal(
                                fresh=True,
                                all_accounts_exhausted=False,
                                weekly_utilization=0.0,
                                short_utilization=0.1,
                                seconds_to_weekly_reset=7 * 24 * 3600,
                            ),
                        )
                        if real_pressure
                        else nullcontext()
                    ),
                    (
                        patch(
                            "teatree.core.agent_admission.read_machine_signal",
                            return_value=MachineSignal(cores=8, load1=1, ram_available_gb=20),
                        )
                        if real_pressure
                        else nullcontext()
                    ),
                    patch("teatree.core.task_dispatch.headless_admission_block_reason", return_value=None),
                    patch("teatree.core.task_dispatch.enqueue_execution") as enqueue,
                    patch("teatree.loop.scanners.architectural_review.announce_held"),
                ):
                    if real_pressure:
                        verdict = agent_admission_verdict()
                        assert verdict.expensive_lane.ceiling == 8
                        assert verdict.expensive_denied is not None
                    if blocked:
                        with self.assertLogs("teatree.loop.scanners.phase_cadence", level="WARNING") as logs:
                            result = _run(loop=loop)
                        assert any("machine HALT" in line for line in logs.output)
                    else:
                        result = _run(loop=loop)

                task = Task.objects.filter(phase=phase).first()
                assert task is not None, result
                assert (task.admitted_at is None) is blocked
                if blocked:
                    assert enqueue.call_count == 0
                    fingerprint = (
                        "architectural-review-held:t3-teatree"
                        if loop == "arch_review"
                        else f"cadence-held:{phase}:t3-teatree"
                    )
                    assert KnownIssue.objects.open().filter(fingerprint=fingerprint).exists()
                    assert Loop.objects.get(name=loop).last_run_at is None
                else:
                    enqueue.assert_called_once_with(task.pk, phase)
                    assert not KnownIssue.objects.open().filter(fingerprint=f"loop-silent:{loop}").exists()
                    assert Loop.objects.get(name=loop).last_run_at is not None


class TestPerLoopRehomedMasterSteps(django.test.TestCase):
    """The former master-only steps now ride each per-loop tick (master-tick removal)."""

    def _run_won(self, **extra_patches: object) -> None:
        report = TickReport(started_at=dt.datetime.now(dt.UTC))
        with (
            patch.object(LoopLease.objects, "claim_ownership", return_value=(True, "me")),
            patch.object(LoopLease.objects, "acquire", return_value=True),
            patch.object(LoopLease.objects, "release"),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            _run(loop="inbox")

    def test_per_loop_tick_drains_a_pending_reinstall(self) -> None:
        with patch("teatree.loop.self_update_reinstall.drain_pending_reinstall") as drain:
            self._run_won()
        # The reinstall drain rides the per-loop tick (behind the lease guard),
        # not a removed master step.
        drain.assert_called_once()

    def test_per_loop_tick_installs_then_resets_the_schedules_reader(self) -> None:
        with patch("teatree.loop.statusline.set_mini_loop_schedules_reader") as set_reader:
            self._run_won()
        # Installed for the render (the live DB reader), then reset to None so the
        # process-global seam never leaks — so the statusline loop line keeps its
        # per-loop countdowns without a master tick.
        assert set_reader.call_count == 2
        assert set_reader.call_args_list[-1].args == (None,)


class TestPerLoopConnectorIsolation(django.test.TestCase):
    """A per-loop tick preflights ONLY its own overlay — one outage can't take the fleet (LOOP-PR-C)."""

    @staticmethod
    def _seed(name: str, overlay: str, *, enabled: bool = True, last_run_at: dt.datetime | None = None) -> None:
        Loop.objects.create(
            name=name,
            script=f"src/teatree/loops/{name}/loop.py",
            delay_seconds=60,
            overlay=overlay,
            enabled=enabled,
            last_run_at=last_run_at,
        )

    def _run_isolated(self, *, loop: str) -> object:
        report = TickReport(started_at=dt.datetime.now(dt.UTC))
        overlays = {"alpha": _CleanOverlay(), "beta": _SlackDownOverlay()}
        with (
            patch("teatree.core.connector_preflight.get_all_overlays", return_value=overlays),
            patch("teatree.core.overlay_loader.OverlayConfigResolver.all_names", return_value=list(overlays)),
            patch.object(LoopLease.objects, "claim_ownership", return_value=(True, "me")),
            patch.object(LoopLease.objects, "acquire", return_value=True),
            patch.object(LoopLease.objects, "release"),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report) as run_tick,
        ):
            _run(loop=loop)
        return run_tick

    def test_unrelated_overlay_outage_does_not_systemexit_per_loop_tick(self) -> None:
        self._seed("probe-alpha", overlay="alpha")
        run_tick = self._run_isolated(loop="probe-alpha")
        assert run_tick.called

    def test_disabled_loop_on_down_overlay_does_not_systemexit(self) -> None:
        self._seed("probe-beta", overlay="beta", enabled=False)
        run_tick = self._run_isolated(loop="probe-beta")
        assert run_tick.called

    def test_loop_on_own_down_overlay_still_systemexits(self) -> None:
        self._seed("probe-beta", overlay="beta")
        with pytest.raises(SystemExit):
            self._run_isolated(loop="probe-beta")
