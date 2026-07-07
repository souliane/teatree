"""Tests for cli/push_gate_tools.py — the ``t3 tool push-gate`` surface (#122)."""

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.push_gate_tools import _resolve_flag
from teatree.quality.push_gate import WHOLE_TREE_DOCTEST, PushGatePlan, PushGateResult

runner = CliRunner()

_SCOPED = PushGatePlan(
    is_full=False,
    reason="scoped to the diff — no FULL trigger",
    doctest_targets=(Path("src/teatree/core/session.py"),),
    astgrep_scope=(Path("src/teatree/core/session.py"),),
    enabled=True,
)
_FULL = PushGatePlan(
    is_full=True,
    reason="incremental_push_gate is OFF — whole-tree (default-safe)",
    doctest_targets=(WHOLE_TREE_DOCTEST,),
    astgrep_scope=None,
    enabled=False,
)


class TestPlanModes:
    def test_default_prints_human_report(self) -> None:
        with (
            patch("teatree.cli.push_gate_tools._resolve_flag", return_value=True),
            patch("teatree.cli.push_gate_tools.resolve_plan", return_value=_SCOPED),
        ):
            result = runner.invoke(app, ["tool", "push-gate"])
        assert result.exit_code == 0
        assert "push-gate: SCOPED" in result.output
        assert "reason:" in result.output

    def test_json_emits_plan(self) -> None:
        with (
            patch("teatree.cli.push_gate_tools._resolve_flag", return_value=True),
            patch("teatree.cli.push_gate_tools.resolve_plan", return_value=_SCOPED),
        ):
            result = runner.invoke(app, ["tool", "push-gate", "--json"])
        assert result.exit_code == 0
        assert '"is_full": false' in result.output
        assert "src/teatree/core/session.py" in result.output

    def test_emit_cmd_prints_doctest_command_and_scope(self) -> None:
        with (
            patch("teatree.cli.push_gate_tools._resolve_flag", return_value=True),
            patch("teatree.cli.push_gate_tools.resolve_plan", return_value=_SCOPED),
        ):
            result = runner.invoke(app, ["tool", "push-gate", "--emit-cmd"])
        assert result.exit_code == 0
        assert "--doctest-modules src/teatree/core/session.py" in result.output
        assert "ast-grep scope:" in result.output

    def test_flag_off_reports_full(self) -> None:
        with (
            patch("teatree.cli.push_gate_tools._resolve_flag", return_value=False),
            patch("teatree.cli.push_gate_tools.resolve_plan", return_value=_FULL),
        ):
            result = runner.invoke(app, ["tool", "push-gate"])
        assert result.exit_code == 0
        assert "push-gate: FULL" in result.output


class TestResolveFlag:
    def test_defaults_to_false(self) -> None:
        # Real resolution under the test Django settings — the flag defaults FALSE.
        assert _resolve_flag() is False

    def test_fails_safe_to_false_when_bootstrap_raises(self) -> None:
        with patch("teatree.cli.push_gate_tools.ensure_django", side_effect=RuntimeError("boom")):
            assert _resolve_flag() is False


class TestRunMode:
    def test_run_exit_zero_when_clean(self) -> None:
        ok = PushGateResult(ok=True, doctest_ok=True, astgrep_findings=(), astgrep_deferred=False, notes=("clean",))
        with (
            patch("teatree.cli.push_gate_tools._resolve_flag", return_value=True),
            patch("teatree.cli.push_gate_tools.resolve_plan", return_value=_SCOPED),
            patch("teatree.cli.push_gate_tools.run_push_gate", return_value=ok),
        ):
            result = runner.invoke(app, ["tool", "push-gate", "--run"])
        assert result.exit_code == 0

    def test_run_exit_nonzero_on_finding(self) -> None:
        finding = {"check_id": "x", "path": "src/teatree/a.py", "start": {"line": 3}}
        bad = PushGateResult(
            ok=False, doctest_ok=True, astgrep_findings=(finding,), astgrep_deferred=False, notes=("bad",)
        )
        with (
            patch("teatree.cli.push_gate_tools._resolve_flag", return_value=True),
            patch("teatree.cli.push_gate_tools.resolve_plan", return_value=_SCOPED),
            patch("teatree.cli.push_gate_tools.run_push_gate", return_value=bad),
        ):
            result = runner.invoke(app, ["tool", "push-gate", "--run"])
        assert result.exit_code == 1
        assert "src/teatree/a.py:3" in result.output
