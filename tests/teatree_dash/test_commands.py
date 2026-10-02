"""The allowlisted command runner: allowlist gate, argv build, timeout handling (#3162)."""

import threading
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models.loop import Loop
from teatree.dash import commands


def test_unknown_key_is_refused() -> None:
    with pytest.raises(commands.CommandNotAllowedError):
        commands.run_allowlisted("rm-rf")


def test_loop_tick_requires_a_loop_name() -> None:
    with pytest.raises(commands.CommandNotAllowedError):
        commands.run_allowlisted("loop-tick", loop_name="")


def test_allowlisted_command_captures_output() -> None:
    completed = CompletedProcess(args=["t3", "doctor", "check"], returncode=0, stdout="ok\n", stderr="")
    with patch.object(commands, "run_allowed_to_fail", return_value=completed) as run:
        result = commands.run_allowlisted("doctor")
    assert run.call_args.args[0] == ["t3", "doctor", "check"]
    assert result.exit_code == 0
    assert result.output == "ok\n"
    assert result.timed_out is False


def test_timeout_returns_flagged_result_not_raise() -> None:
    with patch.object(
        commands, "run_allowed_to_fail", side_effect=TimeoutExpired(cmd=["t3"], timeout=1, output="partial")
    ):
        result = commands.run_allowlisted("doctor")
    assert result.timed_out is True
    assert "partial" in result.output


def test_command_buttons_lists_the_allowlist() -> None:
    keys = {spec.key for spec in commands.command_buttons()}
    assert "doctor" in keys
    assert "loop-tick" in keys


class ConcurrencyGuardTestCase(TestCase):
    """DASH-6: concurrent allowlisted command runs are bounded, never stacked.

    An in-flight command blocks its worker thread for up to the timeout, so a re-clicked
    in-flight command is deduped and total concurrent runs are capped below the pool.
    """

    def _blocking_run(self, started: threading.Event, release: threading.Event):
        def _run(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
            started.set()
            assert release.wait(timeout=5), "test timed out waiting to release the in-flight command"
            return CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        return _run

    def test_second_run_of_in_flight_command_is_rejected(self) -> None:
        started, release = threading.Event(), threading.Event()
        with patch.object(commands, "run_allowed_to_fail", side_effect=self._blocking_run(started, release)):
            worker = threading.Thread(target=lambda: commands.run_allowlisted("doctor"))
            worker.start()
            try:
                assert started.wait(timeout=5), "the first run never entered the subprocess"
                with pytest.raises(commands.CommandBusyError):
                    commands.run_allowlisted("doctor")
            finally:
                release.set()
                worker.join(timeout=5)

    def test_key_runs_again_after_the_in_flight_run_finishes(self) -> None:
        # The guard releases on completion — it is a busy-signal, not a permanent lock.
        started, release = threading.Event(), threading.Event()
        with patch.object(commands, "run_allowed_to_fail", side_effect=self._blocking_run(started, release)):
            worker = threading.Thread(target=lambda: commands.run_allowlisted("doctor"))
            worker.start()
            assert started.wait(timeout=5)
            release.set()
            worker.join(timeout=5)
        completed = CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr="")
        with patch.object(commands, "run_allowed_to_fail", return_value=completed):
            assert commands.run_allowlisted("doctor").exit_code == 0

    def test_concurrent_runs_are_capped_below_the_thread_pool(self) -> None:
        # Distinct commands still starve the pool if unbounded; the cap keeps threads free.
        started, release = threading.Event(), threading.Event()
        Loop.objects.create(name="caploop", script="teatree.loops.review", delay_seconds=60)
        keys = list(commands.ALLOWED_COMMANDS)[: commands._MAX_CONCURRENT_RUNS]
        with patch.object(commands, "run_allowed_to_fail", side_effect=self._blocking_run(started, release)):
            workers = [threading.Thread(target=lambda k=k: commands.run_allowlisted(k)) for k in keys]
            for worker in workers:
                worker.start()
                assert started.wait(timeout=5)
                started.clear()
            try:
                with pytest.raises(commands.CommandBusyError):
                    commands.run_allowlisted("loop-tick", loop_name="caploop")
            finally:
                release.set()
                for worker in workers:
                    worker.join(timeout=5)


class LoopTickValidationTestCase(TestCase):
    """The needs-loop command validates its loop_name against registered loops (#3164, HARDENING #5)."""

    def test_loop_tick_appends_a_registered_loop_name(self) -> None:
        Loop.objects.create(name="dashtickloop", script="teatree.loops.review", delay_seconds=60)
        completed = CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(commands, "run_allowed_to_fail", return_value=completed) as run:
            commands.run_allowlisted("loop-tick", loop_name="dashtickloop")
        assert run.call_args.args[0] == ["t3", "loops", "tick", "--loop", "dashtickloop"]

    def test_loop_tick_rejects_unregistered_loop(self) -> None:
        # An unregistered loop name must never reach the subprocess.
        with (
            patch.object(commands, "run_allowed_to_fail") as run,
            pytest.raises(commands.CommandNotAllowedError),
        ):
            commands.run_allowlisted("loop-tick", loop_name="not-a-real-loop")
        run.assert_not_called()
