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
from unittest.mock import patch

import django.test
import pytest
from django.core.management import call_command

from teatree.core.management.commands.loops_tick import Command, _ScopedJobsBuilder
from teatree.core.models import Loop, LoopLease, Prompt, Worktree
from teatree.core.overlay import OverlayBase, OverlayConnectors, ProvisionStep
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.tick import TickReport
from teatree.loops.base import MiniLoop
from teatree.loops.deadlined_tick import TICK_SUBPROCESS_ENV_MARKER


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
            signals=[ScanSignal(kind="architectural_review.held", summary="review lane held")],
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
                    signals=[ScanSignal(kind="eval_local.held", summary="reserve held")],
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
                return [ScanSignal(kind=kind, summary=kind)]

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

    def test_empty_triage_does_not_recheck_before_its_cadence(self) -> None:
        class _Scanner:
            name = "triage_assessor"
            scans = 0

            def scan(self) -> list[ScanSignal]:
                self.scans += 1
                return [ScanSignal(kind="triage_assessor.held", summary="no issues")]

        scanner = _Scanner()
        prompt = Prompt.objects.create(name="triage-cadence-prompt", body="test")
        Loop.objects.get_or_create(name="triage_assessor", defaults={"delay_seconds": 3600, "prompt": prompt})
        Loop.objects.filter(name="triage_assessor").update(delay_seconds=3600, last_run_at=None)
        mini = MiniLoop(
            name="triage_assessor",
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
            _run(loop="triage_assessor")
            assert Loop.objects.get(name="triage_assessor").last_run_at is not None
            _run(loop="triage_assessor")

        assert scanner.scans == 1


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
