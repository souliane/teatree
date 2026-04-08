"""Tests for the structured step execution engine."""

import subprocess
from functools import partial
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.overlay import ProvisionStep
from teatree.core.step_runner import (
    ProvisionReport,
    StepResult,
    run_callable_step,
    run_provision_steps,
    run_step,
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
    @patch("teatree.core.step_runner.subprocess")
    def test_success(self, mock_sp: MagicMock) -> None:
        mock_sp.run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
        mock_sp.TimeoutExpired = subprocess.TimeoutExpired
        result = run_step("echo-test", ["echo", "hello"])
        assert result.success is True
        assert result.name == "echo-test"
        assert result.duration > 0

    @patch("teatree.core.step_runner.subprocess")
    def test_failure_with_check(self, mock_sp: MagicMock) -> None:
        mock_sp.run.return_value = MagicMock(returncode=1, stdout="", stderr="bad thing")
        mock_sp.TimeoutExpired = subprocess.TimeoutExpired
        result = run_step("fail-step", ["false"], check=True)
        assert result.success is False
        assert "bad thing" in result.error

    @patch("teatree.core.step_runner.subprocess")
    def test_failure_without_check(self, mock_sp: MagicMock) -> None:
        mock_sp.run.return_value = MagicMock(returncode=1, stdout="", stderr="ignored")
        mock_sp.TimeoutExpired = subprocess.TimeoutExpired
        result = run_step("soft-fail", ["false"], check=False)
        assert result.success is True  # check=False means non-zero is OK

    @patch("teatree.core.step_runner.subprocess")
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
