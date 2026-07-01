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
from django_typer.management import TyperCommand

from teatree.core.management.commands.loop_runner import Command
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

    def test_command_is_a_typer_command_owning_the_cadence(self) -> None:
        # The mgmt command is a django-typer TyperCommand exposing ``handle`` and
        # advertising itself as the OS-cron-free cadence owner (#2876).
        assert issubclass(Command, TyperCommand)
        assert callable(Command.handle)
        assert "no OS cron" in Command.help

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
