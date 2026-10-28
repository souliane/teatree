"""``loops_tick`` samples its own CPU into the idle or busy column of the loop it ran (#4999)."""

import datetime as dt
import io
import os
from collections.abc import Generator
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import django.test
import pytest
from django.core.management import call_command

from teatree.core.models import Loop, LoopLease
from teatree.loop.dispatch import dispatch
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.tick import DispatchAction, TickReport
from teatree.loops.deadlined_tick import TICK_SUBPROCESS_ENV_MARKER


@contextmanager
def _worker_tick(report: TickReport, *, owns_lease: bool = True) -> Generator[MagicMock]:
    with (
        patch.dict("os.environ", {TICK_SUBPROCESS_ENV_MARKER: "1"}),
        patch("os._exit") as hard_exit,
        patch.object(LoopLease.objects, "claim_ownership", return_value=(owns_lease, "me")),
        patch.object(LoopLease.objects, "acquire", return_value=True),
        patch.object(LoopLease.objects, "release"),
        patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
        patch("teatree.loop.tick.run_tick", return_value=report),
    ):
        yield hard_exit


class TestTickCostRecording(django.test.TestCase):
    def setUp(self) -> None:
        self.loop = Loop.objects.create(name="probe", script="src/teatree/loops/probe/loop.py", delay_seconds=60)

    def _run_tick(self, report: TickReport, *, owns_lease: bool = True) -> dict[str, bool]:
        with _worker_tick(report, owns_lease=owns_lease):
            call_command("loops_tick", loop="probe", stdout=io.StringIO(), stderr=io.StringIO())
        self.loop.refresh_from_db()
        return {"idle": self.loop.idle_cpu_seconds is not None, "busy": self.loop.busy_cpu_seconds is not None}

    def test_a_tick_with_no_signal_and_no_action_is_idle(self) -> None:
        sampled = self._run_tick(TickReport(started_at=dt.datetime.now(dt.UTC)))

        assert sampled == {"idle": True, "busy": False}

    def test_a_standing_signal_with_a_statusline_action_is_idle(self) -> None:
        signal = ScanSignal(kind="ticket.active", summary="active")
        report = TickReport(started_at=dt.datetime.now(dt.UTC), signals=[signal], actions=dispatch([signal]))

        assert report.action_count == 1
        assert report.actions[0].kind == "statusline"
        assert self._run_tick(report) == {"idle": True, "busy": False}

    def test_a_tick_that_dispatched_an_agent_is_busy(self) -> None:
        report = TickReport(
            started_at=dt.datetime.now(dt.UTC),
            actions=[DispatchAction(kind="agent", zone="t3:reviewer", detail="review")],
        )

        assert self._run_tick(report) == {"idle": False, "busy": True}

    def test_a_lease_lost_skip_is_idle(self) -> None:
        sampled = self._run_tick(TickReport(started_at=dt.datetime.now(dt.UTC)), owns_lease=False)

        assert sampled == {"idle": True, "busy": False}

    def test_the_cost_is_recorded_before_the_worker_hard_exit(self) -> None:
        idle_when_exiting: list[float | None] = []

        def _read_cost(status: int) -> None:
            idle_when_exiting.append(Loop.objects.get(name="probe").idle_cpu_seconds)

        with _worker_tick(TickReport(started_at=dt.datetime.now(dt.UTC))) as hard_exit:
            hard_exit.side_effect = _read_cost
            call_command("loops_tick", loop="probe", stdout=io.StringIO(), stderr=io.StringIO())

        assert hard_exit.call_count == 1
        assert idle_when_exiting[0] is not None

    def test_the_cost_is_recorded_after_the_report(self) -> None:
        class ReportOutput(io.StringIO):
            def write(self, text: str) -> int:
                if "ran" in text:
                    assert Loop.objects.get(name="probe").idle_cpu_seconds is None
                return super().write(text)

        with _worker_tick(TickReport(started_at=dt.datetime.now(dt.UTC))):
            call_command("loops_tick", loop="probe", stdout=io.StringIO(), stderr=ReportOutput())

        self.loop.refresh_from_db()
        assert self.loop.idle_cpu_seconds is not None

    def test_a_crashed_tick_still_records_its_cpu(self) -> None:
        with (
            _worker_tick(TickReport(started_at=dt.datetime.now(dt.UTC))),
            patch("teatree.loop.tick.run_tick", side_effect=RuntimeError("crashed")),
            pytest.raises(RuntimeError, match="crashed"),
        ):
            call_command("loops_tick", loop="probe", stdout=io.StringIO(), stderr=io.StringIO())

        self.loop.refresh_from_db()
        assert self.loop.idle_cpu_seconds is not None

    def test_a_connector_preflight_exit_still_records_its_cpu(self) -> None:
        with (
            patch.dict(os.environ, {TICK_SUBPROCESS_ENV_MARKER: "1"}),
            patch("teatree.loops.connector_preflight.run_loop_connector_preflight", side_effect=SystemExit(3)),
            pytest.raises(SystemExit) as caught,
        ):
            call_command("loops_tick", loop="probe", stdout=io.StringIO(), stderr=io.StringIO())

        assert caught.value.code == 3
        self.loop.refresh_from_db()
        assert self.loop.idle_cpu_seconds is not None
