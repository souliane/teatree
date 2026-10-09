"""Tests for cli/push_gate_tools.py — the ``t3 tool push-gate`` surface (#122)."""

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app
from teatree.quality.push_gate import PushGatePlan, PushGateResult

runner = CliRunner()

_SCOPED = PushGatePlan(
    is_full=False,
    reason="scoped to the diff — no FULL trigger",
    doctest_targets=(Path("src/teatree/core/session.py"),),
    astgrep_scope=(Path("src/teatree/core/session.py"),),
)


class TestPlanModes:
    def test_default_prints_human_report(self) -> None:
        with (
            patch("teatree.cli.push_gate_tools.resolve_plan", return_value=_SCOPED),
        ):
            result = runner.invoke(app, ["tool", "push-gate"])
        assert result.exit_code == 0
        assert "push-gate: SCOPED" in result.output
        assert "reason:" in result.output

    def test_json_emits_plan(self) -> None:
        with (
            patch("teatree.cli.push_gate_tools.resolve_plan", return_value=_SCOPED),
        ):
            result = runner.invoke(app, ["tool", "push-gate", "--json"])
        assert result.exit_code == 0
        assert '"is_full": false' in result.output
        assert "src/teatree/core/session.py" in result.output

    def test_emit_cmd_prints_doctest_command_and_scope(self) -> None:
        with (
            patch("teatree.cli.push_gate_tools.resolve_plan", return_value=_SCOPED),
        ):
            result = runner.invoke(app, ["tool", "push-gate", "--emit-cmd"])
        assert result.exit_code == 0
        assert "--doctest-modules src/teatree/core/session.py" in result.output
        assert "ast-grep scope:" in result.output
        # #3808: the printed reproduction must strip the variable the gate's own
        # child strips, or it reproduces a different run than the one that failed.
        assert "env -uDJANGO_SETTINGS_MODULE" in result.output

    def test_emit_cmd_refuses_to_print_a_runnable_command_with_no_targets(self) -> None:
        empty = PushGatePlan(
            is_full=False,
            reason="no src module changed",
            doctest_targets=(),
            astgrep_scope=(),
        )
        with (
            patch("teatree.cli.push_gate_tools.resolve_plan", return_value=empty),
        ):
            result = runner.invoke(app, ["tool", "push-gate", "--emit-cmd"])
        assert result.exit_code == 0
        assert "--doctest-modules" not in result.output, "a bare command would fall back to pytest's own testpaths"
        assert "none" in result.output


class TestRunMode:
    def test_run_exit_zero_when_clean(self) -> None:
        ok = PushGateResult(
            ok=True,
            doctest_ok=True,
            astgrep_findings=(),
            astgrep_deferred=False,
            notes=("clean",),
            exit_code=0,
        )
        with (
            patch("teatree.cli.push_gate_tools.resolve_plan", return_value=_SCOPED),
            patch("teatree.cli.push_gate_tools.run_push_gate", return_value=ok),
        ):
            result = runner.invoke(app, ["tool", "push-gate", "--run"])
        assert result.exit_code == 0

    def test_run_exit_nonzero_on_finding(self) -> None:
        finding = {"check_id": "x", "path": "src/teatree/a.py", "start": {"line": 3}}
        bad = PushGateResult(
            ok=False,
            doctest_ok=True,
            astgrep_findings=(finding,),
            astgrep_deferred=False,
            notes=("bad",),
            exit_code=1,
        )
        with (
            patch("teatree.cli.push_gate_tools.resolve_plan", return_value=_SCOPED),
            patch("teatree.cli.push_gate_tools.run_push_gate", return_value=bad),
        ):
            result = runner.invoke(app, ["tool", "push-gate", "--run"])
        assert result.exit_code == 1
        assert "src/teatree/a.py:3" in result.output

    def test_run_preserves_a_signal_style_exit_from_the_doctest_sweep(self) -> None:
        aborted = PushGateResult(
            ok=False,
            doctest_ok=False,
            astgrep_findings=(),
            astgrep_deferred=False,
            notes=("doctest process exited -9",),
            exit_code=137,
        )
        with (
            patch("teatree.cli.push_gate_tools.resolve_plan", return_value=_SCOPED),
            patch("teatree.cli.push_gate_tools.run_push_gate", return_value=aborted),
        ):
            result = runner.invoke(app, ["tool", "push-gate", "--run"])

        assert result.exit_code == 137
