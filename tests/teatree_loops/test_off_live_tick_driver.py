"""teatree.loops.off_live_tick_driver — the driver for the loops every other driver skips.

``directive_loop`` / ``dream`` / ``outer_loop`` are ``off_live_tick``, so
``build_loop_table_jobs`` skips them and ``timer_chain_loop_names`` builds them no chain;
the "own low-frequency cron" their docstrings promised was never installed on any host.
This chain is that driver, and it stays inside the worker's no-OS-scheduler contract.
Integration-first against the real DB + ``django_tasks_db`` backend, with the deadlined
subprocess runner stubbed so no real tick is spawned.
"""

import contextlib
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
from teatree.loops.timer_chains import LoopRunnerState

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

    def _queued_run_names(self) -> set[str]:
        rows = DBTaskResult.objects.filter(task_path=off_live_tick_driver.run_off_live_tick_loop.module_path)
        return {row.args_kwargs["args"][0] for row in rows}

    def test_queues_one_own_task_per_off_live_tick_loop(self) -> None:
        """Each loop is its OWN queue entry, never three inline on one executor thread.

        Run back to back inline, the three heaviest passes in the tree could hold a
        single ``loops`` thread for 3 x the per-command ceiling — half the floor-sized
        reactive pool on a 2-core box, so every ``loop_timer`` fire and the reconcile
        chain that repairs them waited behind one slow ``dream``.
        """
        result = off_live_tick_driver.drive_off_live_tick_loops.func()

        assert self._queued_run_names() == {"directive_loop", "dream", "outer_loop"}
        assert result == {"queued": 3, "deduped": 0}

    def test_each_queued_task_runs_its_loops_deadlined_tick_command(self) -> None:
        with unittest.mock.patch.object(off_live_tick_driver, "run_deadlined_argv", self._record):
            result = off_live_tick_driver.run_off_live_tick_loop.func("dream")

        assert result == {"driven": 1, "timed_out": 0}
        (argv, deadline) = self._ran[0]
        assert argv[1:3] == ["-m", "teatree"]
        assert tuple(argv[-2:]) == ("dream", "tick")
        assert deadline == off_live_tick_driver.DEADLINE_SECONDS

    def test_a_queued_task_reports_a_timed_out_tick(self) -> None:
        def _timeout(argv: list[str], *, label: str, deadline: float) -> TickOutcome:
            return {"timed_out": True, "returncode": None}

        with unittest.mock.patch.object(off_live_tick_driver, "run_deadlined_argv", _timeout):
            assert off_live_tick_driver.run_off_live_tick_loop.func("dream") == {"driven": 1, "timed_out": 1}

    def test_a_queued_task_for_a_vanished_loop_is_a_no_op(self) -> None:
        with unittest.mock.patch.object(off_live_tick_driver, "run_deadlined_argv", self._record):
            assert off_live_tick_driver.run_off_live_tick_loop.func("gone") == {"unknown": 1}
        assert self._ran == []

    def test_does_not_stack_a_second_run_behind_an_in_flight_one(self) -> None:
        """A ``dream`` pass outliving several drive intervals must not queue one run apiece."""
        off_live_tick_driver.run_off_live_tick_loop.enqueue("dream")

        result = off_live_tick_driver.drive_off_live_tick_loops.func()

        assert result == {"queued": 2, "deduped": 1}
        dream_runs = [
            row
            for row in DBTaskResult.objects.filter(task_path=off_live_tick_driver.run_off_live_tick_loop.module_path)
            if row.args_kwargs["args"] == ["dream"]
        ]
        assert len(dream_runs) == 1

    def test_reschedules_itself_before_running_the_ticks(self) -> None:
        with (
            unittest.mock.patch.object(off_live_tick_driver, "off_live_tick_commands", side_effect=_ExplodingTickError),
            contextlib.suppress(_ExplodingTickError),
        ):
            off_live_tick_driver.drive_off_live_tick_loops.func()

        pending = DBTaskResult.objects.filter(
            task_path=off_live_tick_driver.drive_off_live_tick_loops.module_path, status=TaskResultStatus.READY
        )
        assert pending.count() == 1, "successor-first: a raising body must never orphan the chain"

    def test_self_dedups_against_a_pending_fire(self) -> None:
        off_live_tick_driver.drive_off_live_tick_loops.using(run_after=timezone.now()).enqueue()

        result = off_live_tick_driver.drive_off_live_tick_loops.func()

        assert result == {"deduped": 1}
        assert self._queued_run_names() == set()

    def test_kill_switch_off_halts_the_chain_without_driving_anything(self) -> None:
        with unittest.mock.patch.object(
            off_live_tick_driver, "read_loop_runner_state", return_value=LoopRunnerState.OFF
        ):
            result = off_live_tick_driver.drive_off_live_tick_loops.func()

        assert result == {"halted": 1}
        assert self._queued_run_names() == set()
        assert not DBTaskResult.objects.filter(
            task_path=off_live_tick_driver.drive_off_live_tick_loops.module_path, status=TaskResultStatus.READY
        ).exists()

    def test_an_unreadable_kill_switch_re_arms_the_chain_instead_of_ending_it(self) -> None:
        """A read that cannot CONFIRM the switch is not an OFF (F7), and this chain is never re-headed.

        ``loop_timer``'s equivalent halt is repaired by ``ensure_loop_timers`` within
        five minutes; this chain is seeded only from the maintenance seeder, so one
        transient DB blip used to leave dream / directive_loop / outer_loop with no
        driver at all — with ``driverless_loops`` blind to it, because it checks only
        whether an ``off_tick_command`` is declared.
        """
        with unittest.mock.patch.object(
            off_live_tick_driver, "read_loop_runner_state", return_value=LoopRunnerState.UNREADABLE
        ):
            result = off_live_tick_driver.drive_off_live_tick_loops.func()

        assert result == {"unconfirmed": 1}
        assert self._queued_run_names() == set(), "an unconfirmable switch drives nothing"
        pending = DBTaskResult.objects.filter(
            task_path=off_live_tick_driver.drive_off_live_tick_loops.module_path, status=TaskResultStatus.READY
        )
        assert pending.count() == 1, "the chain must survive a blip it cannot read through"

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
