"""Tests for ``t3 dogfood overlay-provision-smoke`` (#1308).

The smoke management command shells out to ``t3 <overlay> ...`` in
production. Tests inject a fake :class:`StepRunner` so the suite never
actually executes any subprocess — the live run would take minutes and
require Docker / overlay infra.

We also verify the DM-on-failure plumbing (``notify_user`` is called
with a body naming the failing step and command) without making a real
Slack call.
"""

from unittest.mock import patch

import pytest
import typer

from teatree.core.management.commands.dogfood import Command as DogfoodCommand
from teatree.loop.dogfood_smoke import SmokeOutcomeKind, SmokeReport, SmokeStep, StepResult

pytestmark = pytest.mark.django_db


def _call_smoke(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[str, int]:
    """Invoke the inner ``overlay_provision_smoke`` subcommand directly.

    django-typer wraps the bound command in a proxy that swallows
    ``typer.Exit`` codes (normalising every non-zero outcome to 1), so we
    can't assert the categorised exit codes via the proxy's ``CliRunner``
    path. Calling the method directly preserves the
    ``typer.Exit(code=N)`` raise so the test can inspect the actual code.
    """
    cmd = DogfoodCommand()
    kwargs = _parse_args(args)
    code = 0
    try:
        cmd.overlay_provision_smoke(**kwargs)
    except typer.Exit as exc:
        code = int(exc.exit_code or 0)
    captured = capsys.readouterr()
    return captured.out, code


def _parse_args(args: tuple[str, ...]) -> dict[str, object]:
    """Tiny arg parser for the test harness — covers the flags we exercise."""
    kwargs: dict[str, object] = {
        "overlay": "teatree",
        "fixture_ticket_url": "https://github.com/souliane/teatree/issues/1308",
        "variant": "",
        "dry_run": False,
        "notify_on_failure": True,
    }
    for arg in args:
        if arg == "--dry-run":
            kwargs["dry_run"] = True
        elif arg == "--no-notify-on-failure":
            kwargs["notify_on_failure"] = False
        elif arg == "--notify-on-failure":
            kwargs["notify_on_failure"] = True
    return kwargs


class TestDryRun:
    def test_dry_run_lists_steps_without_executing(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("teatree.core.management.commands.dogfood.run_smoke") as mock_run:
            out, code = _call_smoke(capsys, "--dry-run")

        assert code == 0
        assert "dry-run" in out
        assert "workspace_ticket" in out
        assert "worktree_provision" in out
        assert "worktree_teardown" in out
        # The orchestrator must NOT be invoked in dry-run mode.
        mock_run.assert_not_called()


class TestSmokeExecution:
    def test_all_green_exits_zero_and_no_dm(self, capsys: pytest.CaptureFixture[str]) -> None:
        report = SmokeReport(outcome=SmokeOutcomeKind.PASS, steps=[])
        with (
            patch("teatree.core.management.commands.dogfood.run_smoke", return_value=report),
            patch("teatree.core.management.commands.dogfood._notify_failure") as mock_notify,
        ):
            out, code = _call_smoke(capsys)

        assert code == 0
        assert "PASS" in out
        mock_notify.assert_not_called()

    def test_provision_failure_exits_eleven_and_dms_user(self, capsys: pytest.CaptureFixture[str]) -> None:
        step = SmokeStep(name="worktree_provision", command=("t3", "teatree", "worktree", "provision"))
        result = StepResult(
            step=step,
            returncode=1,
            stderr="dslr alias missing: test-variant",
            stdout="",
            elapsed_seconds=0.1,
        )
        report = SmokeReport(
            outcome=SmokeOutcomeKind.PROVISION_FAILED,
            failing_step="worktree_provision",
            steps=[result],
        )
        with (
            patch("teatree.core.management.commands.dogfood.run_smoke", return_value=report),
            patch("teatree.core.management.commands.dogfood._notify_failure") as mock_notify,
        ):
            out, code = _call_smoke(capsys)

        assert code == 11
        assert "provision_failed" in out
        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["failing_step"] == "worktree_provision"
        assert "t3 teatree worktree provision" in kwargs["command_str"]
        assert "dslr alias missing" in kwargs["stderr"]

    def test_start_failure_exits_twelve(self, capsys: pytest.CaptureFixture[str]) -> None:
        step = SmokeStep(name="worktree_start", command=("t3", "teatree", "worktree", "start"))
        result = StepResult(step=step, returncode=1, stderr="boom", stdout="", elapsed_seconds=0.1)
        report = SmokeReport(outcome=SmokeOutcomeKind.START_FAILED, failing_step="worktree_start", steps=[result])
        with (
            patch("teatree.core.management.commands.dogfood.run_smoke", return_value=report),
            patch("teatree.core.management.commands.dogfood._notify_failure"),
        ):
            _, code = _call_smoke(capsys)
        assert code == 12

    def test_ready_failure_exits_thirteen(self, capsys: pytest.CaptureFixture[str]) -> None:
        step = SmokeStep(name="worktree_ready", command=("t3", "teatree", "worktree", "ready"))
        result = StepResult(step=step, returncode=1, stderr="health 503", stdout="", elapsed_seconds=0.1)
        report = SmokeReport(outcome=SmokeOutcomeKind.READY_FAILED, failing_step="worktree_ready", steps=[result])
        with (
            patch("teatree.core.management.commands.dogfood.run_smoke", return_value=report),
            patch("teatree.core.management.commands.dogfood._notify_failure"),
        ):
            _, code = _call_smoke(capsys)
        assert code == 13

    def test_timeout_exits_sixteen(self, capsys: pytest.CaptureFixture[str]) -> None:
        step = SmokeStep(name="worktree_start", command=("t3", "teatree", "worktree", "start"))
        result = StepResult(step=step, returncode=-1, stderr="", stdout="", elapsed_seconds=120.0, timed_out=True)
        report = SmokeReport(outcome=SmokeOutcomeKind.TIMEOUT, failing_step="worktree_start", steps=[result])
        with (
            patch("teatree.core.management.commands.dogfood.run_smoke", return_value=report),
            patch("teatree.core.management.commands.dogfood._notify_failure"),
        ):
            _, code = _call_smoke(capsys)
        assert code == 16

    def test_no_notify_flag_suppresses_dm_on_failure(self, capsys: pytest.CaptureFixture[str]) -> None:
        step = SmokeStep(name="worktree_provision", command=("t3", "teatree", "worktree", "provision"))
        result = StepResult(step=step, returncode=1, stderr="boom", stdout="", elapsed_seconds=0.1)
        report = SmokeReport(
            outcome=SmokeOutcomeKind.PROVISION_FAILED,
            failing_step="worktree_provision",
            steps=[result],
        )
        with (
            patch("teatree.core.management.commands.dogfood.run_smoke", return_value=report),
            patch("teatree.core.management.commands.dogfood._notify_failure") as mock_notify,
        ):
            _, code = _call_smoke(capsys, "--no-notify-on-failure")

        assert code == 11
        mock_notify.assert_not_called()


class TestExitCodeMapping:
    def _report(self, outcome: SmokeOutcomeKind) -> SmokeReport:
        return SmokeReport(outcome=outcome, failing_step="x", steps=[])

    @pytest.mark.parametrize(
        ("outcome", "expected_code"),
        [
            (SmokeOutcomeKind.PASS, 0),
            (SmokeOutcomeKind.PROVISION_FAILED, 11),
            (SmokeOutcomeKind.START_FAILED, 12),
            (SmokeOutcomeKind.READY_FAILED, 13),
            (SmokeOutcomeKind.TEARDOWN_FAILED, 14),
            (SmokeOutcomeKind.CLEAN_FAILED, 15),
            (SmokeOutcomeKind.TIMEOUT, 16),
            (SmokeOutcomeKind.UNKNOWN, 19),
        ],
    )
    def test_outcome_maps_to_distinct_exit_code(self, outcome: SmokeOutcomeKind, expected_code: int) -> None:
        from teatree.core.management.commands.dogfood import _exit_code_for  # noqa: PLC0415

        assert _exit_code_for(outcome) == expected_code


class TestNotifyFailureBody:
    def test_body_includes_failing_step_command_and_stderr_tail(self) -> None:
        from teatree.core.management.commands.dogfood import _dm_failure_body  # noqa: PLC0415

        body = _dm_failure_body(
            "dogfood smoke provision_failed at worktree_provision",
            failing_step="worktree_provision",
            command_str="t3 teatree worktree provision",
            stderr="line 1\nline 2\nline 3\nfinal: dslr alias missing",
        )

        assert "worktree_provision" in body
        assert "t3 teatree worktree provision" in body
        assert "dslr alias missing" in body
        # Markdown code fence for the stderr block — Slack renders it as
        # a monospace block.
        assert "```" in body
