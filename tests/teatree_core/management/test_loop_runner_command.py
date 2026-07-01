"""manage.py loop_runner — the singleton loop-runner daemon command (#2876).

The ``loop-runner`` flock singleton gives at-most-one runner per box: a second
invocation while one is alive refuses with a non-zero exit. ``--once`` runs a
single beat + drain and returns (the foreground / test variant). The pid path is
isolated to a per-test temp dir so parallel xdist workers never contend on a
shared ``DATA_DIR`` pidfile.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import django.test
import pytest
from django.core.management import call_command

from teatree.loops.runner import LOOP_RUNNER_SINGLETON
from teatree.utils.singleton import singleton


class TestLoopRunnerCommand(django.test.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pid_path = Path(tmp.name) / f"{LOOP_RUNNER_SINGLETON}.pid"
        patcher = patch("teatree.utils.singleton.default_pid_path", lambda _name: pid_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_runner_refused_while_singleton_held(self) -> None:
        with singleton(LOOP_RUNNER_SINGLETON), pytest.raises(SystemExit) as exc:
            call_command("loop_runner")
        assert exc.value.code == 1

    def test_once_runs_a_single_beat_then_drains_and_returns(self) -> None:
        # One real (silent — no admitted loops) beat + one drain, then returns,
        # having acquired and released the singleton. The queue drain is django-tasks'
        # own batch Worker (its ``BEGIN EXCLUSIVE`` cannot nest inside TestCase's
        # transaction), so it is stubbed here — its wiring is asserted, its internals
        # are django-tasks' concern.
        with patch("teatree.loops.runner.drain_loop_queue") as drain:
            call_command("loop_runner", once=True)
        drain.assert_called_once()
