"""``manage.py dream`` — orchestration for the idle-time dream cron (#1933).

The command owns the cron mechanics around the (stubbed) distillation engine:
the in-flight ``LoopLease`` lock so two passes never overlap, the cadence gate
for ``tick``, the ``DreamRunMarker`` stamping (success vs. attempt), and the
``--dry-run`` no-write path. These are all testable without an LLM because the
engine is a typed seam.
"""

import datetime as dt
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ConsolidatedMemory, DreamRunMarker, LoopLease, MiniLoopMarker
from teatree.loops.dream.engine import DreamRunResult
from teatree.loops.dream.loop import DREAM_LEASE_NAME, DREAM_LEASE_SECONDS, DREAM_LOOP_NAME


def _ok_result(*, dry_run: bool = False) -> DreamRunResult:
    return DreamRunResult(clusters_recorded=1, members_replayed=3, dry_run=dry_run)


class DreamRunStampsMarkerTestCase(TestCase):
    def test_run_stamps_marker_succeeded(self) -> None:
        before = timezone.now()
        with patch(
            "teatree.loops.dream.engine.run_consolidation",
            return_value=_ok_result(),
        ):
            call_command("dream", "run", stdout=StringIO())
        marker = DreamRunMarker.objects.get(name=DreamRunMarker.NAME)
        assert marker.last_succeeded_at is not None
        assert marker.last_succeeded_at >= before
        assert marker.last_attempted_at == marker.last_succeeded_at

    def test_run_clears_staleness(self) -> None:
        # A stale engine (never succeeded) is the bootstrap state.
        assert DreamRunMarker.objects.is_stale(timezone.now()) is True
        with patch(
            "teatree.loops.dream.engine.run_consolidation",
            return_value=_ok_result(),
        ):
            call_command("dream", "run", stdout=StringIO())
        assert DreamRunMarker.objects.is_stale(timezone.now()) is False

    def test_failed_run_bumps_attempt_only_keeps_stale(self) -> None:
        with patch(
            "teatree.loops.dream.engine.run_consolidation",
            side_effect=RuntimeError("engine boom"),
        ):
            call_command("dream", "run", stdout=StringIO())
        marker = DreamRunMarker.objects.get(name=DreamRunMarker.NAME)
        assert marker.last_attempted_at is not None
        assert marker.last_succeeded_at is None
        assert DreamRunMarker.objects.is_stale(timezone.now()) is True


class DreamDryRunTestCase(TestCase):
    def test_dry_run_writes_no_marker_and_no_rows(self) -> None:
        called: dict[str, object] = {}

        def _capture(*, overlay: str, since: object, dry_run: bool, eval_proposals: object = None) -> DreamRunResult:
            called["dry_run"] = dry_run
            called["eval_proposals"] = eval_proposals
            return _ok_result(dry_run=dry_run)

        with patch("teatree.loops.dream.engine.run_consolidation", side_effect=_capture):
            call_command("dream", "run", "--dry-run", stdout=StringIO())

        assert called["dry_run"] is True
        assert called["eval_proposals"] is None
        assert not DreamRunMarker.objects.exists()
        assert ConsolidatedMemory.objects.count() == 0

    def test_dry_run_writes_no_marker_when_engine_raises(self) -> None:
        # A dry-run promises "no rows or marker written" — even an attempt
        # marker must not be stamped when the engine raises under --dry-run.
        stdout = StringIO()
        with patch(
            "teatree.loops.dream.engine.run_consolidation",
            side_effect=RuntimeError("engine boom"),
        ):
            call_command("dream", "run", "--dry-run", stdout=stdout)
        assert "FAIL" in stdout.getvalue()
        assert not DreamRunMarker.objects.exists()


class DreamProposeEvalsFlagTestCase(TestCase):
    @staticmethod
    def _capture(seen: dict[str, object]):
        def _run(*, overlay: str, since: object, dry_run: bool, eval_proposals: object = None) -> DreamRunResult:
            seen["eval_proposals"] = eval_proposals
            return _ok_result()

        return _run

    def test_propose_evals_off_by_default(self) -> None:
        seen: dict[str, object] = {}
        with patch("teatree.loops.dream.engine.run_consolidation", side_effect=self._capture(seen)):
            call_command("dream", "run", stdout=StringIO())
        assert seen["eval_proposals"] is None

    def test_propose_evals_flag_enables_the_phase(self) -> None:
        seen: dict[str, object] = {}
        with patch("teatree.loops.dream.engine.run_consolidation", side_effect=self._capture(seen)):
            call_command("dream", "run", "--propose-evals", stdout=StringIO())
        assert seen["eval_proposals"] is not None

    def test_env_enables_the_phase_for_the_cadence_tick(self) -> None:
        seen: dict[str, object] = {}
        with (
            patch("teatree.loops.dream.engine.run_consolidation", side_effect=self._capture(seen)),
            patch.dict("os.environ", {"T3_DREAM_PROPOSE_EVALS": "1"}),
        ):
            call_command("dream", "run", stdout=StringIO())
        assert seen["eval_proposals"] is not None


class DreamLeaseTtlTestCase(TestCase):
    def test_run_acquires_lease_sized_to_the_pass_budget(self) -> None:
        # The default 120s lease would expire under a wall-clock-capped pass and
        # let a concurrent pass win the CAS. The command must size the lease to
        # the pass budget so "no two overlapping passes" holds for the whole pass.
        captured: dict[str, object] = {}
        real_acquire = LoopLease.objects.acquire

        def _spy(name: str, *, owner: str, lease_seconds: int = 120) -> bool:
            captured["name"] = name
            captured["lease_seconds"] = lease_seconds
            return real_acquire(name, owner=owner, lease_seconds=lease_seconds)

        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch.object(type(LoopLease.objects), "acquire", side_effect=_spy),
        ):
            call_command("dream", "run", stdout=StringIO())

        assert captured["name"] == DREAM_LEASE_NAME
        assert captured["lease_seconds"] == DREAM_LEASE_SECONDS


class DreamInFlightLockTestCase(TestCase):
    def test_overlapping_run_skips_when_lease_held(self) -> None:
        # Simulate a concurrent pass already holding the lease.
        assert LoopLease.objects.acquire(DREAM_LEASE_NAME, owner="other-pid")
        stdout = StringIO()
        with patch("teatree.loops.dream.engine.run_consolidation") as engine:
            call_command("dream", "run", stdout=stdout)
        engine.assert_not_called()
        assert "SKIP" in stdout.getvalue()
        # The loser never stamps a marker.
        assert not DreamRunMarker.objects.exists()

    def test_lease_released_after_run(self) -> None:
        with patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()):
            call_command("dream", "run", stdout=StringIO())
        # A fresh run can re-acquire — the lease was released in finally.
        assert LoopLease.objects.acquire(DREAM_LEASE_NAME, owner="next-pid")

    def test_lease_released_even_when_engine_raises(self) -> None:
        with patch("teatree.loops.dream.engine.run_consolidation", side_effect=RuntimeError("boom")):
            call_command("dream", "run", stdout=StringIO())
        assert LoopLease.objects.acquire(DREAM_LEASE_NAME, owner="after-failure")


class DreamTickCadenceTestCase(TestCase):
    def test_tick_runs_when_cadence_elapsed(self) -> None:
        with patch(
            "teatree.loops.dream.engine.run_consolidation",
            return_value=_ok_result(),
        ) as engine:
            call_command("dream", "tick", stdout=StringIO())
        engine.assert_called_once()
        assert MiniLoopMarker.objects.filter(name=DREAM_LOOP_NAME).exists()

    def test_tick_skips_when_cadence_not_elapsed(self) -> None:
        MiniLoopMarker.objects.mark_fired(DREAM_LOOP_NAME, timezone.now())
        stdout = StringIO()
        with patch("teatree.loops.dream.engine.run_consolidation") as engine:
            call_command("dream", "tick", stdout=stdout)
        engine.assert_not_called()
        assert "SKIP" in stdout.getvalue()

    def test_run_ignores_cadence_gate(self) -> None:
        # `run` is the manual escape hatch — it runs regardless of cadence.
        MiniLoopMarker.objects.mark_fired(DREAM_LOOP_NAME, timezone.now())
        with patch(
            "teatree.loops.dream.engine.run_consolidation",
            return_value=_ok_result(),
        ) as engine:
            call_command("dream", "run", stdout=StringIO())
        engine.assert_called_once()

    def test_tick_failed_engine_does_not_advance_cadence_ledger(self) -> None:
        with patch(
            "teatree.loops.dream.engine.run_consolidation",
            side_effect=RuntimeError("engine boom"),
        ):
            call_command("dream", "tick", stdout=StringIO())
        assert not MiniLoopMarker.objects.filter(name=DREAM_LOOP_NAME).exists()


class DreamZeroMembersFailLoudTestCase(TestCase):
    def test_zero_members_does_not_stamp_succeeded(self) -> None:
        zero_result = DreamRunResult(clusters_recorded=0, members_replayed=0, dry_run=False)
        with patch("teatree.loops.dream.engine.run_consolidation", return_value=zero_result):
            call_command("dream", "run", stdout=StringIO())
        marker = DreamRunMarker.objects.filter(name=DreamRunMarker.NAME).first()
        assert marker is None or marker.last_succeeded_at is None

    def test_zero_members_stamps_attempted(self) -> None:
        zero_result = DreamRunResult(clusters_recorded=0, members_replayed=0, dry_run=False)
        with patch("teatree.loops.dream.engine.run_consolidation", return_value=zero_result):
            call_command("dream", "run", stdout=StringIO())
        marker = DreamRunMarker.objects.filter(name=DreamRunMarker.NAME).first()
        assert marker is not None
        assert marker.last_attempted_at is not None

    def test_zero_members_emits_warn(self) -> None:
        zero_result = DreamRunResult(clusters_recorded=0, members_replayed=0, dry_run=False)
        stdout = StringIO()
        with patch("teatree.loops.dream.engine.run_consolidation", return_value=zero_result):
            call_command("dream", "run", stdout=stdout)
        assert "WARN" in stdout.getvalue()

    def test_zero_members_keeps_staleness_alarm_active(self) -> None:
        zero_result = DreamRunResult(clusters_recorded=0, members_replayed=0, dry_run=False)
        with patch("teatree.loops.dream.engine.run_consolidation", return_value=zero_result):
            call_command("dream", "run", stdout=StringIO())
        assert DreamRunMarker.objects.is_stale(timezone.now()) is True

    def test_nonzero_members_stamps_succeeded(self) -> None:
        with patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()):
            call_command("dream", "run", stdout=StringIO())
        marker = DreamRunMarker.objects.get(name=DreamRunMarker.NAME)
        assert marker.last_succeeded_at is not None


class DreamSinceTestCase(TestCase):
    def test_run_passes_since_to_engine(self) -> None:
        captured: dict[str, object] = {}

        def _capture(
            *, overlay: str, since: dt.datetime | None, dry_run: bool, eval_proposals: object = None
        ) -> DreamRunResult:
            captured["since"] = since
            return _ok_result()

        with patch("teatree.loops.dream.engine.run_consolidation", side_effect=_capture):
            call_command("dream", "run", "--since", "2026-06-01T00:00:00+00:00", stdout=StringIO())

        since = captured["since"]
        assert isinstance(since, dt.datetime)
        assert since == dt.datetime(2026, 6, 1, tzinfo=dt.UTC)

    def test_naive_since_is_normalized_to_aware(self) -> None:
        # `--since 2026-06-01` (no tz) would flow into the USE_TZ engine as a
        # naive datetime and TypeError on comparison with timezone.now().
        captured: dict[str, object] = {}

        def _capture(
            *, overlay: str, since: dt.datetime | None, dry_run: bool, eval_proposals: object = None
        ) -> DreamRunResult:
            captured["since"] = since
            return _ok_result()

        with patch("teatree.loops.dream.engine.run_consolidation", side_effect=_capture):
            call_command("dream", "run", "--since", "2026-06-01", stdout=StringIO())

        since = captured["since"]
        assert isinstance(since, dt.datetime)
        assert timezone.is_aware(since)

    def test_malformed_since_raises_command_error(self) -> None:
        with (
            patch("teatree.loops.dream.engine.run_consolidation") as engine,
            pytest.raises(CommandError),
        ):
            call_command("dream", "run", "--since", "not-a-date", stdout=StringIO())
        engine.assert_not_called()
