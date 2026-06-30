"""Tests for the structured step execution engine."""

import subprocess
import threading
from functools import partial
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.overlay import ProvisionStep
from teatree.core.step_runner import ProvisionReport, StepResult, run_callable_step, run_provision_steps, run_step
from tests.teatree_core._provision_timebox_stub import (
    BROKEN_DEPENDENCY_NAME,
    provision_timebox_internally_broken,
    provision_timebox_unimportable,
)


class TestStepResult(TestCase):
    def test_summary_shows_ok_for_success(self) -> None:
        result = StepResult(name="test-step", success=True, duration=1.5)
        assert "[OK]" in result.summary()
        assert "test-step" in result.summary()

    def test_summary_shows_failed_with_error(self) -> None:
        result = StepResult(name="broken-step", success=False, duration=0.3, error="kaboom")
        assert "[FAILED]" in result.summary()
        assert "kaboom" in result.summary()


class TestProvisionReport(TestCase):
    def test_empty_report_is_successful(self) -> None:
        report = ProvisionReport()
        assert report.success is True
        assert report.failed_step is None

    def test_all_passing_steps(self) -> None:
        report = ProvisionReport(
            steps=[
                StepResult(name="a", success=True),
                StepResult(name="b", success=True),
            ]
        )
        assert report.success is True
        assert report.failed_step is None

    def test_one_failing_step(self) -> None:
        report = ProvisionReport(
            steps=[
                StepResult(name="a", success=True),
                StepResult(name="b", success=False, error="fail"),
            ]
        )
        assert report.success is False
        assert report.failed_step == "b"

    def test_summary_shows_count(self) -> None:
        report = ProvisionReport(
            steps=[
                StepResult(name="a", success=True),
                StepResult(name="b", success=False, error="oops"),
            ]
        )
        summary = report.summary()
        assert "1/2 steps succeeded" in summary
        assert "First failure: b" in summary


class TestRunStep(TestCase):
    @patch("teatree.utils.run.subprocess")
    def test_success(self, mock_sp: MagicMock) -> None:
        mock_sp.run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
        mock_sp.TimeoutExpired = subprocess.TimeoutExpired
        result = run_step("echo-test", ["echo", "hello"])
        assert result.success is True
        assert result.name == "echo-test"
        assert result.duration > 0

    @patch("teatree.utils.run.subprocess")
    def test_failure_with_check(self, mock_sp: MagicMock) -> None:
        mock_sp.run.return_value = MagicMock(returncode=1, stdout="", stderr="bad thing")
        mock_sp.TimeoutExpired = subprocess.TimeoutExpired
        result = run_step("fail-step", ["false"], check=True)
        assert result.success is False
        assert "bad thing" in result.error

    @patch("teatree.utils.run.subprocess")
    def test_failure_without_check(self, mock_sp: MagicMock) -> None:
        mock_sp.run.return_value = MagicMock(returncode=1, stdout="", stderr="ignored")
        mock_sp.TimeoutExpired = subprocess.TimeoutExpired
        result = run_step("soft-fail", ["false"], check=False)
        assert result.success is True  # check=False means non-zero is OK

    @patch("teatree.utils.run.subprocess")
    def test_timeout(self, mock_sp: MagicMock) -> None:
        mock_sp.run.side_effect = subprocess.TimeoutExpired(cmd=["slow"], timeout=1)
        mock_sp.TimeoutExpired = subprocess.TimeoutExpired
        result = run_step("slow-step", ["sleep", "999"], timeout=1)
        assert result.success is False
        assert "timed out" in result.error

    @pytest.mark.integration
    def test_command_not_found(self) -> None:
        result = run_step("missing", ["nonexistent_binary_xyz123"])
        assert result.success is False
        assert "command not found" in result.error


class TestRunStepSurvivesMissingProvisionTimebox(TestCase):
    """souliane/teatree#2664 — ``run_step`` degrades when ``provision_timebox`` is absent.

    Teardown runs the WORKTREE's own checkout (``uv --directory <worktree>
    run``). A worktree whose base predates ``provision_timebox`` (#2220) cannot
    import it, so the lazy ``from teatree.core.provision_timebox import
    run_timeboxed_step`` in ``run_step`` raised ``ModuleNotFoundError`` and
    aborted the whole teardown runner mid-stream — skipping every step ordered
    after the abort (DB drop, ``Worktree`` row delete, docker down). The
    optional time-box enhancement must degrade to a plain subprocess run, never
    abort the caller.
    """

    @patch("teatree.utils.run.subprocess")
    def test_run_step_does_not_raise_when_module_absent(self, mock_sp: MagicMock) -> None:
        mock_sp.run.return_value = MagicMock(returncode=0, stdout="hooks\n", stderr="")
        mock_sp.TimeoutExpired = subprocess.TimeoutExpired

        with provision_timebox_unimportable():
            result = run_step("git-hooks-path", ["git", "rev-parse", "--git-path", "hooks"], check=False)

        assert result.success is True
        assert result.name == "git-hooks-path"
        assert "hooks" in result.stdout

    @patch("teatree.utils.run.subprocess")
    def test_run_step_soft_fail_stays_benign_when_module_absent(self, mock_sp: MagicMock) -> None:
        mock_sp.run.return_value = MagicMock(returncode=1, stdout="", stderr="ignored")
        mock_sp.TimeoutExpired = subprocess.TimeoutExpired

        with provision_timebox_unimportable():
            result = run_step("soft-fail", ["false"], check=False)

        assert result.success is True

    @patch("teatree.utils.run.subprocess")
    def test_run_step_command_not_found_surfaced_when_module_absent(self, mock_sp: MagicMock) -> None:
        mock_sp.run.side_effect = FileNotFoundError("no such file: nope")
        mock_sp.TimeoutExpired = subprocess.TimeoutExpired

        with provision_timebox_unimportable():
            result = run_step("missing", ["nope"], check=False)

        assert result.success is False
        assert "command not found" in result.error

    @patch("teatree.utils.run.subprocess")
    def test_run_step_timeout_surfaced_when_module_absent(self, mock_sp: MagicMock) -> None:
        mock_sp.run.side_effect = subprocess.TimeoutExpired(cmd=["slow"], timeout=1)
        mock_sp.TimeoutExpired = subprocess.TimeoutExpired

        with provision_timebox_unimportable():
            result = run_step("slow", ["sleep", "999"], timeout=1, check=False)

        assert result.success is False
        assert "timed out" in result.error

    def test_run_step_propagates_when_module_present_but_internally_broken(self) -> None:
        """A present ``provision_timebox`` failing on its OWN broken import must NOT be swallowed.

        The catch is narrowed to the module's own absence (``ModuleNotFoundError.name`` ==
        ``teatree.core.provision_timebox``). A present-but-internally-broken module raises a
        ``ModuleNotFoundError`` whose ``.name`` is the missing DEPENDENCY, so ``run_step`` must
        re-raise it rather than silently degrading to a plain run — which would disable the
        timeout/heartbeat/alert for every healthy install and mask the real bug.
        """
        with provision_timebox_internally_broken(), pytest.raises(ModuleNotFoundError) as exc_info:
            run_step("probe", ["true"], check=False)

        assert exc_info.value.name == BROKEN_DEPENDENCY_NAME


class TestRunCallableStep(TestCase):
    def test_success_with_plain_callable(self) -> None:
        result = run_callable_step("noop", lambda: None)
        assert result.success is True

    def test_exception_is_caught(self) -> None:
        def boom() -> None:
            msg = "kaboom"
            raise RuntimeError(msg)

        result = run_callable_step("boom", boom)
        assert result.success is False
        assert "kaboom" in result.error

    def test_subprocess_completed_process_success(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
        result = run_callable_step("sp-ok", lambda: completed)
        assert result.success is True
        assert result.stdout == "ok"

    def test_subprocess_completed_process_failure(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="err")
        result = run_callable_step("sp-fail", lambda: completed)
        assert result.success is False
        assert "err" in result.error


class TestRunProvisionSteps(TestCase):
    def test_runs_all_steps(self) -> None:
        results: list[str] = []
        steps = [
            ProvisionStep(name="a", callable=partial(results.append, "a")),
            ProvisionStep(name="b", callable=partial(results.append, "b")),
        ]
        report = run_provision_steps(steps)
        assert results == ["a", "b"]
        assert report.success is True
        assert len(report.steps) == 2

    def test_halts_on_required_failure(self) -> None:
        results: list[str] = []

        def fail() -> None:
            msg = "broken"
            raise RuntimeError(msg)

        steps = [
            ProvisionStep(name="ok", callable=partial(results.append, "ok"), required=True),
            ProvisionStep(name="fail", callable=fail, required=True),
            ProvisionStep(name="skipped", callable=partial(results.append, "skipped"), required=True),
        ]
        report = run_provision_steps(steps, stop_on_required_failure=True)
        assert results == ["ok"]  # "skipped" never ran
        assert report.success is False
        assert report.failed_step == "fail"

    def test_continues_past_optional_failure(self) -> None:
        results: list[str] = []

        def fail() -> None:
            msg = "optional-fail"
            raise RuntimeError(msg)

        steps = [
            ProvisionStep(name="ok", callable=partial(results.append, "ok"), required=True),
            ProvisionStep(name="opt-fail", callable=fail, required=False),
            ProvisionStep(name="after", callable=partial(results.append, "after"), required=True),
        ]
        report = run_provision_steps(steps, stop_on_required_failure=True)
        assert results == ["ok", "after"]  # continued past optional failure
        assert len(report.steps) == 3

    def test_verbose_output(self) -> None:
        output: list[str] = []
        steps = [
            ProvisionStep(name="verbose-step", callable=lambda: None),
        ]
        run_provision_steps(
            steps,
            verbose=True,
            stdout_writer=output.append,
            stderr_writer=output.append,
        )
        assert any("verbose-step" in line for line in output)

    def test_verbose_shows_stdout_from_subprocess(self) -> None:
        stdout_lines: list[str] = []

        def step_with_stdout() -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="line1\nline2", stderr="")

        steps = [ProvisionStep(name="stdout-step", callable=step_with_stdout)]
        run_provision_steps(steps, verbose=True, stdout_writer=stdout_lines.append)
        assert any("line1" in line for line in stdout_lines)
        assert any("line2" in line for line in stdout_lines)

    def test_verbose_shows_stderr_from_failed_subprocess(self) -> None:
        stderr_lines: list[str] = []

        def step_with_stderr() -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="err1\nerr2")

        steps = [ProvisionStep(name="stderr-step", callable=step_with_stderr)]
        run_provision_steps(steps, verbose=True, stderr_writer=stderr_lines.append)
        assert any("err1" in line for line in stderr_lines)
        assert any("err2" in line for line in stderr_lines)

    def test_failed_required_step_property(self) -> None:
        report = ProvisionReport(
            steps=[
                StepResult(name="ok", success=True),
                StepResult(name="broken", success=False, error="fail"),
            ]
        )
        assert report.failed_required_step == "broken"

    def test_failed_required_step_none_when_all_pass(self) -> None:
        report = ProvisionReport(steps=[StepResult(name="ok", success=True)])
        assert report.failed_required_step is None


class TestRunProvisionStepsTimebox(TestCase):
    """A blocking callable provision step aborts loud (#2244).

    A step that blocks on its inner subprocess (a hung `compose run`, a missing
    DB source) is wall-clock bounded and fails with the named step instead of
    hanging the whole provision.
    """

    @patch("teatree.core.provision_timebox.notify_user")
    @patch("teatree.core.provision_timebox.resolve_step_timeout_seconds", return_value=0.1)
    def test_blocking_required_step_aborts_loud(self, mock_ceiling: MagicMock, mock_notify: MagicMock) -> None:
        release = threading.Event()
        ran_after: list[str] = []
        steps = [
            ProvisionStep(name="seed", callable=lambda: release.wait(timeout=3), required=True),
            ProvisionStep(name="after", callable=partial(ran_after.append, "after"), required=True),
        ]
        report = run_provision_steps(steps)
        release.set()

        assert report.success is False
        assert report.failed_step == "seed"
        assert "timed out" in report.steps[0].error
        assert ran_after == []  # halted on the timed-out required step
        assert mock_notify.called


class TestRunProvisionStepsSurvivesMissingProvisionTimebox(TestCase):
    """The callable path degrades to a plain run when the time-box is absent (#2664).

    A worktree torn down from a stale base cannot import ``provision_timebox``;
    the callable provision path must degrade, never abort the caller.
    """

    def test_callable_step_runs_when_module_absent(self) -> None:
        ran: list[str] = []
        steps = [ProvisionStep(name="noop", callable=partial(ran.append, "noop"), required=True)]

        with provision_timebox_unimportable():
            report = run_provision_steps(steps)

        assert ran == ["noop"]
        assert report.success is True

    def test_callable_step_propagates_when_module_present_but_internally_broken(self) -> None:
        steps = [ProvisionStep(name="noop", callable=lambda: None, required=True)]

        with provision_timebox_internally_broken(), pytest.raises(ModuleNotFoundError) as exc_info:
            run_provision_steps(steps)

        assert exc_info.value.name == BROKEN_DEPENDENCY_NAME
