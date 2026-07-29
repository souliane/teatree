"""teatree.loops.off_live_tick_driver — the driver for the loops every other driver skips.

``directive_loop`` / ``dream`` / ``outer_loop`` are ``off_live_tick``, so
``build_loop_table_jobs`` skips them and ``timer_chain_loop_names`` builds them no chain;
the "own low-frequency cron" their docstrings promised was never installed on any host.
This chain is that driver, and it stays inside the worker's no-OS-scheduler contract.
Integration-first against the real DB + ``django_tasks_db`` backend, with the deadlined
subprocess runner stubbed so no real tick is spawned.
"""

import unittest.mock

import django.test
from django.utils import timezone
from django_tasks.base import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from teatree.loops import off_live_tick_driver
from teatree.loops.deadlined_tick import TickOutcome, run_deadlined_argv
from teatree.loops.off_live_tick_driver import (
    drive_off_live_tick_loops,
    ensure_off_live_tick_driver_chain,
    off_live_tick_commands,
)

_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops"]}}


class _ExplodingTickError(RuntimeError):
    """Raised by a fake tick runner to prove a body fault never orphans the chain."""


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestOffLiveTickDriver(django.test.TestCase):
    """The driver for the loops the live fan-out excludes — they had no driver at all.

    ``directive_loop`` / ``dream`` / ``outer_loop`` are ``off_live_tick``, so
    ``build_loop_table_jobs`` skips them and ``timer_chain_loop_names`` builds them no
    chain; the "own low-frequency cron" their docstrings promised was never installed on
    any host. This chain is that driver, and it stays inside the worker's no-OS-scheduler
    contract.
    """

    def setUp(self) -> None:
        DBTaskResult.objects.all().delete()
        self._ran: list[tuple[list[str], float]] = []

    def _record(self, argv: list[str], *, label: str, deadline: float) -> TickOutcome:
        self._ran.append((argv, deadline))
        return {"timed_out": False, "returncode": 0}

    def test_fires_every_registered_off_live_tick_loops_own_tick_command(self) -> None:
        with unittest.mock.patch.object(off_live_tick_driver, "run_deadlined_argv", self._record):
            result = off_live_tick_driver.drive_off_live_tick_loops.func()

        driven = {tuple(argv[-2:]) for argv, _deadline in self._ran}
        assert driven == {("directive", "tick"), ("dream", "tick"), ("outer", "tick")}
        assert result["driven"] == 3
        # Each runs as its own `python -m teatree <cmd> tick` subprocess, deadlined.
        for argv, deadline in self._ran:
            assert argv[1:3] == ["-m", "teatree"]
            assert deadline == off_live_tick_driver.DEADLINE_SECONDS

    def test_reschedules_itself_before_running_the_ticks(self) -> None:
        def _explode(argv: list[str], *, label: str, deadline: float) -> TickOutcome:
            raise _ExplodingTickError

        with unittest.mock.patch.object(off_live_tick_driver, "run_deadlined_argv", _explode):
            off_live_tick_driver.drive_off_live_tick_loops.func()

        pending = DBTaskResult.objects.filter(
            task_path=off_live_tick_driver.drive_off_live_tick_loops.module_path, status=TaskResultStatus.READY
        )
        assert pending.count() == 1, "successor-first: a raising body must never orphan the chain"

    def test_self_dedups_against_a_pending_fire(self) -> None:
        off_live_tick_driver.drive_off_live_tick_loops.using(run_after=timezone.now()).enqueue()
        with unittest.mock.patch.object(off_live_tick_driver, "run_deadlined_argv", self._record):
            result = off_live_tick_driver.drive_off_live_tick_loops.func()
        assert result == {"deduped": 1}
        assert self._ran == []

    def test_kill_switch_off_halts_the_chain_without_driving_anything(self) -> None:
        with (
            unittest.mock.patch.object(off_live_tick_driver, "loop_runner_enabled", return_value=False),
            unittest.mock.patch.object(off_live_tick_driver, "run_deadlined_argv", self._record),
        ):
            result = off_live_tick_driver.drive_off_live_tick_loops.func()

        assert result == {"halted": 1}
        assert self._ran == []
        assert not DBTaskResult.objects.filter(
            task_path=off_live_tick_driver.drive_off_live_tick_loops.module_path, status=TaskResultStatus.READY
        ).exists()

    def test_counts_a_timed_out_tick_without_aborting_the_rest(self) -> None:
        def _timeout(argv: list[str], *, label: str, deadline: float) -> TickOutcome:
            return {"timed_out": True, "returncode": None}

        with unittest.mock.patch.object(off_live_tick_driver, "run_deadlined_argv", _timeout):
            result = off_live_tick_driver.drive_off_live_tick_loops.func()

        assert result == {"driven": 3, "timed_out": 3}

    def test_the_driver_is_the_real_deadlined_subprocess_runner(self) -> None:
        # The production seam, unstubbed: the driver calls the shared deadlined runner.
        assert off_live_tick_driver.run_deadlined_argv is run_deadlined_argv


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestDeclaredCommands(django.test.TestCase):
    """Every off-live-tick loop declares the tick command the chain fires."""

    def test_the_real_registry_declares_all_three_tick_commands(self) -> None:
        assert dict(off_live_tick_commands()) == {
            "directive_loop": ("directive", "tick"),
            "dream": ("dream", "tick"),
            "outer_loop": ("outer", "tick"),
        }


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestEnsureChain(django.test.TestCase):
    """Seeding is idempotent, so a worker restart re-arms without duplicating."""

    def setUp(self) -> None:
        DBTaskResult.objects.all().delete()

    def test_seeds_one_head_and_stays_idempotent(self) -> None:
        ensure_off_live_tick_driver_chain()
        ensure_off_live_tick_driver_chain()
        assert DBTaskResult.objects.filter(task_path=drive_off_live_tick_loops.module_path).count() == 1
