"""teatree.loops.deadlined_tick — the shared deadlined-subprocess tick runner.

The worker-shutdown kill surface (in-flight tick groups are tracked and killed) and the
deadlined-subprocess + whole-group-kill contract, with argv stubbed to shell tools so no
real tick is spawned.
"""

import os

import django.test
import pytest
from django.utils import timezone

from teatree.loops import deadlined_tick
from teatree.loops.deadlined_tick import kill_live_tick_process_groups, run_deadlined_argv, run_deadlined_tick
from teatree.utils.run import Popen, TimeoutExpired, spawn_session_leader
from teatree.utils.singleton import pid_alive


class TestLiveTickProcessGroups(django.test.SimpleTestCase):
    """The worker-shutdown kill surface: in-flight tick groups are tracked + killed."""

    def setUp(self) -> None:
        deadlined_tick._LIVE_TICK_PGIDS.clear()  # process-global registry — isolate from other tests

    def test_kill_live_tick_process_groups_kills_a_registered_group(self) -> None:
        proc = spawn_session_leader(["sleep", "30"])  # a stand-in in-flight tick
        pgid = os.getpgid(proc.pid)
        deadlined_tick._register_tick_pgid(pgid)
        try:
            assert pid_alive(proc.pid)
            killed = kill_live_tick_process_groups()
            assert pgid in killed
            proc.wait(timeout=5)
            assert not pid_alive(proc.pid)
        finally:
            deadlined_tick._unregister_tick_pgid(pgid)
            deadlined_tick._killpg(pgid)

    def test_completed_tick_leaves_no_group_registered(self) -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(deadlined_tick, "_tick_argv", lambda name: ["true"])
            run_deadlined_tick("x", deadline=30)
        assert kill_live_tick_process_groups() == []  # nothing leaked past the tick


class TestRunDeadlinedTick(django.test.SimpleTestCase):
    """The deadlined-subprocess + whole-group-kill contract, argv stubbed to shell tools."""

    def test_success_returns_returncode(self) -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(deadlined_tick, "_tick_argv", lambda name: ["true"])
            outcome = run_deadlined_tick("x", deadline=30)
        assert outcome == {"timed_out": False, "returncode": 0}

    def test_the_live_tick_carries_the_subprocess_env_marker(self) -> None:
        # ``loops_tick`` hard-exits only on the marker the spawned subprocess carries,
        # so an in-process ``call_command`` (tests) never trips it.
        seen: dict[str, str] = {}

        def _capture(
            argv: list[str], *, label: str, deadline: float, env: dict[str, str]
        ) -> deadlined_tick.TickOutcome:
            seen.update(env)
            return {"timed_out": False, "returncode": 0}

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(deadlined_tick, "run_deadlined_argv", _capture)
            run_deadlined_tick("x", deadline=30)
        assert seen[deadlined_tick.TICK_SUBPROCESS_ENV_MARKER] == "1"

    def test_deadline_kills_the_process_group(self) -> None:
        started = timezone.now()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(deadlined_tick, "_tick_argv", lambda name: ["sleep", "30"])
            outcome = run_deadlined_tick("x", deadline=0.3)
        elapsed = (timezone.now() - started).total_seconds()
        assert outcome["timed_out"] is True
        assert elapsed < 10  # the deadline fired and killed the group, not waited out the sleep

    def test_run_deadlined_argv_inherits_the_environment_when_none_is_given(self) -> None:
        # The off-live-tick driver passes no env, so the tick command sees this process's.
        outcome = run_deadlined_argv(["true"], label="probe", deadline=30)
        assert outcome == {"timed_out": False, "returncode": 0}


class _StubProc:
    """A duck-typed ``Popen`` whose ``wait`` can refuse to return, like a group that survives SIGKILL."""

    def __init__(self, pid: int, *, wait_times_out: bool = False) -> None:
        self.pid = pid
        self._wait_times_out = wait_times_out

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_times_out:
            raise TimeoutExpired(cmd=["sleep"], timeout=timeout)
        return 0


class TestGroupKillEdges(django.test.SimpleTestCase):
    """The tolerate-a-dead-child paths: a reaped process, and a group that outlives SIGKILL."""

    @staticmethod
    def _reaped_proc() -> Popen[str]:
        proc = spawn_session_leader(["true"])
        proc.wait(timeout=5)  # reaped, so its pid no longer resolves to a group
        return proc

    def test_pgid_of_a_reaped_process_is_none(self) -> None:
        assert deadlined_tick._tick_pgid(self._reaped_proc()) is None

    def test_killing_an_already_gone_group_is_a_silent_no_op(self) -> None:
        deadlined_tick._kill_process_group(self._reaped_proc())  # no pgid to signal, no raise

    def test_a_group_that_survives_sigkill_is_logged_not_raised(self) -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(deadlined_tick, "_tick_pgid", lambda _proc: 4242)
            mp.setattr(deadlined_tick, "_killpg", lambda _pgid: None)
            with self.assertLogs(deadlined_tick.logger, level="ERROR") as logs:
                deadlined_tick._kill_process_group(_StubProc(4242, wait_times_out=True))
        assert "did not die after SIGKILL" in "\n".join(logs.output)
