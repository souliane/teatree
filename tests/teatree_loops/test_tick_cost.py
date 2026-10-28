"""``loops.tick_cost`` — the CPU reading and the guarded write a worker tick subprocess makes (#4999)."""

import os
from types import SimpleNamespace
from unittest.mock import patch

import django.test
import pytest
from django.db import InterfaceError, OperationalError

from teatree.core.models import Loop
from teatree.loops.deadlined_tick import TICK_SUBPROCESS_ENV_MARKER
from teatree.loops.tick_cost import process_cpu_seconds, record_tick_cost


def test_process_cpu_includes_waited_children() -> None:
    own = SimpleNamespace(ru_utime=1.0, ru_stime=0.5)
    children = SimpleNamespace(ru_utime=0.25, ru_stime=0.75)
    with (
        patch("teatree.loops.tick_cost.resource.RUSAGE_SELF", 0),
        patch("teatree.loops.tick_cost.resource.RUSAGE_CHILDREN", 1),
        patch("teatree.loops.tick_cost.resource.getrusage", side_effect=lambda who: own if who == 0 else children),
    ):
        assert process_cpu_seconds() == pytest.approx(2.5)


class TestRecordTickCost(django.test.TestCase):
    def setUp(self) -> None:
        self.loop = Loop.objects.create(name="probe", script="src/teatree/loops/probe/loop.py", delay_seconds=60)

    def test_without_the_worker_marker_nothing_is_recorded(self) -> None:
        with patch.dict(os.environ):
            os.environ.pop(TICK_SUBPROCESS_ENV_MARKER, None)
            record_tick_cost("probe", found_work=False)

        self.loop.refresh_from_db()
        assert self.loop.idle_cpu_seconds is None

    def test_a_failed_telemetry_write_is_logged_and_never_raised(self) -> None:
        with (
            patch.dict(os.environ, {TICK_SUBPROCESS_ENV_MARKER: "1"}),
            patch.object(Loop.objects, "record_tick_cost", side_effect=OperationalError("database is locked")),
            self.assertLogs("teatree.loops.tick_cost", level="WARNING"),
        ):
            record_tick_cost("probe", found_work=False)

    def test_a_closed_database_connection_does_not_fail_the_tick(self) -> None:
        with (
            patch.dict(os.environ, {TICK_SUBPROCESS_ENV_MARKER: "1"}),
            patch.object(Loop.objects, "record_tick_cost", side_effect=InterfaceError("connection closed")),
            self.assertLogs("teatree.loops.tick_cost", level="WARNING"),
        ):
            record_tick_cost("probe", found_work=False)

    def test_an_error_that_is_not_a_database_error_still_surfaces(self) -> None:
        with (
            patch.dict(os.environ, {TICK_SUBPROCESS_ENV_MARKER: "1"}),
            patch.object(Loop.objects, "record_tick_cost", side_effect=TypeError("bug")),
            pytest.raises(TypeError),
        ):
            record_tick_cost("probe", found_work=False)
