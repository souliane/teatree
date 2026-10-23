"""``loops_tick`` records the tick process's own CPU into the loop's cost EWMA (#4999)."""

import datetime as dt
import io
from collections.abc import Generator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import django.test
import pytest
from django.core.management import call_command
from django.db import OperationalError

from teatree.core.models import Loop, LoopLease
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.tick import TickReport
from teatree.loops.deadlined_tick import TICK_SUBPROCESS_ENV_MARKER

_SELF = SimpleNamespace(ru_utime=1.0, ru_stime=0.5)
_CHILDREN = SimpleNamespace(ru_utime=0.25, ru_stime=0.25)
_TICK_CPU = 2.0


def _getrusage(who: int) -> SimpleNamespace:
    return _SELF if who == 0 else _CHILDREN


@contextmanager
def _tick(report: TickReport, *, owns_lease: bool = True) -> Generator[None]:
    with (
        patch("teatree.loops.tick_cost.resource.getrusage", side_effect=_getrusage),
        patch("teatree.loops.tick_cost.resource.RUSAGE_SELF", 0),
        patch("teatree.loops.tick_cost.resource.RUSAGE_CHILDREN", 1),
        patch.object(LoopLease.objects, "claim_ownership", return_value=(owns_lease, "me")),
        patch.object(LoopLease.objects, "acquire", return_value=True),
        patch.object(LoopLease.objects, "release"),
        patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
        patch("teatree.loop.tick.run_tick", return_value=report),
        patch("teatree.core.management.commands.loops_tick.Command._hard_exit_if_subprocess"),
    ):
        yield


def _run() -> None:
    call_command("loops_tick", loop="probe", stdout=io.StringIO(), stderr=io.StringIO())


class TestTickCostRecording(django.test.TestCase):
    def setUp(self) -> None:
        self.loop = Loop.objects.create(name="probe", script="src/teatree/loops/probe/loop.py", delay_seconds=60)

    def _idle_busy(self) -> tuple[float | None, float | None]:
        self.loop.refresh_from_db()
        return self.loop.idle_cpu_seconds, self.loop.busy_cpu_seconds

    def test_a_tick_with_no_signal_and_no_action_is_idle(self) -> None:
        def cost_is_stored_before_exit() -> None:
            assert self._idle_busy() == (_TICK_CPU, None)

        with (
            _tick(TickReport(started_at=dt.datetime.now(dt.UTC))),
            patch.dict("os.environ", {TICK_SUBPROCESS_ENV_MARKER: "1"}),
            patch(
                "teatree.core.management.commands.loops_tick.Command._hard_exit_if_subprocess",
                side_effect=cost_is_stored_before_exit,
            ) as hard_exit,
        ):
            _run()

        hard_exit.assert_called_once_with()
        assert self._idle_busy() == (_TICK_CPU, None)

    def test_a_tick_that_found_a_signal_is_busy(self) -> None:
        report = TickReport(started_at=dt.datetime.now(dt.UTC), signals=[ScanSignal(kind="x.y", summary="s")])
        with _tick(report), patch.dict("os.environ", {TICK_SUBPROCESS_ENV_MARKER: "1"}):
            _run()

        assert self._idle_busy() == (None, _TICK_CPU)

    def test_a_lease_lost_skip_is_idle(self) -> None:
        with (
            _tick(TickReport(started_at=dt.datetime.now(dt.UTC)), owns_lease=False),
            patch.dict("os.environ", {TICK_SUBPROCESS_ENV_MARKER: "1"}),
        ):
            _run()

        assert self._idle_busy() == (_TICK_CPU, None)

    def test_a_run_without_the_worker_marker_records_nothing(self) -> None:
        with _tick(TickReport(started_at=dt.datetime.now(dt.UTC))), patch.dict("os.environ", clear=False) as env:
            env.pop(TICK_SUBPROCESS_ENV_MARKER, None)
            _run()

        assert self._idle_busy() == (None, None)

    def test_a_failed_telemetry_write_never_fails_the_tick(self) -> None:
        with (
            _tick(TickReport(started_at=dt.datetime.now(dt.UTC))),
            patch.dict("os.environ", {TICK_SUBPROCESS_ENV_MARKER: "1"}),
            patch.object(Loop.objects, "record_tick_cost", side_effect=OperationalError("database is locked")),
            self.assertLogs("teatree.loops.tick_cost", level="WARNING"),
        ):
            _run()

    def test_a_recorder_error_other_than_a_database_error_still_surfaces(self) -> None:
        with (
            _tick(TickReport(started_at=dt.datetime.now(dt.UTC))),
            patch.dict("os.environ", {TICK_SUBPROCESS_ENV_MARKER: "1"}),
            patch.object(Loop.objects, "record_tick_cost", side_effect=TypeError("bug")),
            pytest.raises(TypeError),
        ):
            _run()
