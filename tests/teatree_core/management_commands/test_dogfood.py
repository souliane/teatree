"""Tests for ``t3 dogfood overlay-provision-smoke`` (#1308).

The smoke management command shells out to ``t3 <overlay> ...`` in
production. Tests inject a fake :class:`StepRunner` so the suite never
actually executes any subprocess — the live run would take minutes and
require Docker / overlay infra.

We also verify the DM-on-failure plumbing (``notify_user`` is called
with a body naming the failing step and command) without making a real
Slack call.

The exit-code assertions go through Django's ``call_command`` (the real
production entry path for ``t3 dogfood``) so they pin the *propagated*
code. django-typer swallows a ``typer.Exit`` into a returned value (exit
0); the command therefore ``raise SystemExit(code)`` and these tests use
``pytest.raises(SystemExit)`` to read the genuine code — a ``typer.Exit``
here would regress to a silent exit 0.
"""

from unittest.mock import patch

import pytest
from django.core.management import call_command

from teatree.loop.dogfood_smoke import SmokeOutcomeKind, SmokeReport, SmokeStep, StepResult

pytestmark = pytest.mark.django_db


def _call_smoke(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[str, int]:
    """Invoke ``t3 dogfood overlay-provision-smoke`` via ``call_command``.

    This is the production entry path — django-typer runs the subcommand
    under Django's ``call_command``, which swallows a ``typer.Exit`` into a
    returned value (process exits 0). The command therefore raises
    ``SystemExit(code)`` on a non-zero outcome, so the harness reads the
    propagated code off ``SystemExit`` (and treats a clean return — the PASS
    and dry-run paths — as code 0).
    """
    kwargs = _parse_args(args)
    code = 0
    try:
        call_command("dogfood", "overlay-provision-smoke", **kwargs)
    except SystemExit as exc:
        code = int(exc.code or 0)
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
        elif arg == "--no-overlay":
            kwargs["overlay"] = ""
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

    def test_body_omits_stderr_block_when_stderr_is_blank(self) -> None:
        from teatree.core.management.commands.dogfood import _dm_failure_body  # noqa: PLC0415

        body = _dm_failure_body(
            "dogfood smoke timeout at worktree_start",
            failing_step="worktree_start",
            command_str="t3 teatree worktree start",
            stderr="   \n  \n",  # only whitespace → no stderr section
        )
        assert "worktree_start" in body
        assert "stderr:" not in body
        assert "```" not in body

    def test_body_omits_stderr_block_when_stderr_is_empty(self) -> None:
        from teatree.core.management.commands.dogfood import _dm_failure_body  # noqa: PLC0415

        body = _dm_failure_body(
            "dogfood smoke timeout at worktree_start",
            failing_step="worktree_start",
            command_str="t3 teatree worktree start",
            stderr="",
        )
        assert "stderr:" not in body


class TestNotifyFailureRouting:
    """Exercise the ``_notify_failure`` helper itself (#1308)."""

    def test_notify_failure_routes_to_verified_delivery_wrapper(self) -> None:
        """The smoke-failure DM goes through the #1181 verified-delivery wrapper."""
        from teatree.core.management.commands.dogfood import _notify_failure  # noqa: PLC0415

        with patch("teatree.messaging.notify_with_fallback") as mock_notify:
            _notify_failure(
                summary_text="dogfood smoke provision_failed",
                failing_step="worktree_provision",
                command_str="t3 teatree worktree provision",
                stderr="boom",
            )

        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["idempotency_key"] == "dogfood_smoke:worktree_provision"
        assert "worktree_provision" in mock_notify.call_args.args[0]

    def test_notify_failure_swallows_notify_exception(self) -> None:
        from teatree.core.management.commands.dogfood import _notify_failure  # noqa: PLC0415

        with patch("teatree.messaging.notify_with_fallback", side_effect=RuntimeError("slack down")):
            # Best-effort — the helper must not propagate notify failures.
            _notify_failure(
                summary_text="dogfood smoke provision_failed",
                failing_step="worktree_provision",
                command_str="t3 teatree worktree provision",
                stderr="boom",
            )


class TestOverlayResolution:
    """Cover the active-overlay fallback path (#1308)."""

    def test_missing_overlay_exits_two_and_skips_smoke(self, capsys: pytest.CaptureFixture[str]) -> None:
        """An unresolved overlay short-circuits with exit code 2 and never calls ``run_smoke``."""
        with (
            patch("teatree.core.management.commands.dogfood._resolve_active_overlay", return_value=""),
            patch("teatree.core.management.commands.dogfood.run_smoke") as mock_run,
        ):
            _, code = _call_smoke(capsys, "--no-overlay")

        assert code == 2
        mock_run.assert_not_called()

    def test_resolve_active_overlay_returns_empty_when_no_overlay_registered(self) -> None:
        from teatree.core.management.commands.dogfood import _resolve_active_overlay  # noqa: PLC0415

        with patch("teatree.config.discover_active_overlay", return_value=None):
            assert _resolve_active_overlay() == ""

    def test_resolve_active_overlay_strips_t3_prefix(self) -> None:
        from teatree.core.management.commands.dogfood import _resolve_active_overlay  # noqa: PLC0415

        class _Overlay:
            name = "t3-teatree"

        with patch("teatree.config.discover_active_overlay", return_value=_Overlay()):
            assert _resolve_active_overlay() == "teatree"


class TestFailingStepCommandLookup:
    """Cover the ``command_str`` lookup loop inside the smoke command (#1308)."""

    def test_failing_step_not_in_report_yields_empty_command_str(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Defensive: a corrupted report where ``failing_step`` does not match
        # any captured step's name — the lookup loop falls through without
        # break, leaving ``command_str`` empty.
        step = SmokeStep(name="workspace_ticket", command=("t3", "teatree", "workspace", "ticket"))
        result = StepResult(step=step, returncode=0, stderr="", stdout="", elapsed_seconds=0.01)
        report = SmokeReport(
            outcome=SmokeOutcomeKind.UNKNOWN,
            failing_step="missing_step",
            steps=[result],
        )
        with (
            patch("teatree.core.management.commands.dogfood.run_smoke", return_value=report),
            patch("teatree.core.management.commands.dogfood._notify_failure") as mock_notify,
        ):
            _, code = _call_smoke(capsys)

        # UNKNOWN maps to exit code 19.
        assert code == 19
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["command_str"] == ""

    def test_failing_step_command_propagated_to_notifier(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Two steps in the report — the second is the failing one, so the
        # lookup loop must iterate past the first to find the command_str.
        first_step = SmokeStep(name="workspace_ticket", command=("t3", "teatree", "workspace", "ticket"))
        failing_step = SmokeStep(
            name="worktree_provision",
            command=("t3", "teatree", "worktree", "provision"),
        )
        first_result = StepResult(step=first_step, returncode=0, stderr="", stdout="", elapsed_seconds=0.01)
        failing_result = StepResult(
            step=failing_step,
            returncode=1,
            stderr="missing dslr",
            stdout="",
            elapsed_seconds=0.1,
        )
        report = SmokeReport(
            outcome=SmokeOutcomeKind.PROVISION_FAILED,
            failing_step="worktree_provision",
            steps=[first_result, failing_result],
        )

        with (
            patch("teatree.core.management.commands.dogfood.run_smoke", return_value=report),
            patch("teatree.core.management.commands.dogfood._notify_failure") as mock_notify,
        ):
            _, code = _call_smoke(capsys)

        assert code == 11
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["command_str"] == "t3 teatree worktree provision"
