"""Tests for cli/push_gate_tools.py — the ``t3 tool push-gate`` surface (#122)."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.push_gate_tools import _brief_for_worktree
from teatree.core.models import PlanArtifact, Session, Task, Ticket, Worktree
from teatree.quality.brief_constraints import ConstraintViolation
from teatree.quality.push_gate import PushGatePlan, PushGateResult

runner = CliRunner()


class TestWorktreeBrief(TestCase):
    def test_pending_future_coding_task_does_not_constrain_the_current_push(self) -> None:
        ticket = Ticket.objects.create()
        Worktree.objects.create(
            ticket=ticket,
            overlay="teatree",
            repo_path="souliane/teatree",
            branch="feature",
            extra={"worktree_path": "/tmp/brief-pending-task"},
        )
        session = Session.objects.create(ticket=ticket)
        PlanArtifact.objects.create(ticket=ticket, plan_text="Current plan")
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            status=Task.Status.COMPLETED,
            execution_reason="No migration.",
        )
        future = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            status=Task.Status.PENDING,
            execution_reason="No new setting.",
        )

        brief = _brief_for_worktree(Path("/tmp/brief-pending-task"))

        assert brief == "Current plan\nNo migration."
        assert "No new setting." not in brief

        future.status = Task.Status.CLAIMED
        future.save(update_fields=["status"])
        assert _brief_for_worktree(Path("/tmp/brief-pending-task")) == "Current plan\nNo new setting."

        future.status = Task.Status.FAILED
        future.save(update_fields=["status"])
        assert _brief_for_worktree(Path("/tmp/brief-pending-task")) == "Current plan\nNo migration."

        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="debugging",
            status=Task.Status.COMPLETED,
            execution_reason="No extra migration.",
        )
        assert _brief_for_worktree(Path("/tmp/brief-pending-task")) == "Current plan\nNo extra migration."


def test_brief_read_initializes_isolated_gate_database(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("T3_CONFIG_DB", None)
    env.pop("DATABASE_URL", None)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env["XDG_DATA_HOME"] = str(tmp_path / "data")
    env["T3_CONTROL_DB_DIR"] = str(tmp_path / "control-db")
    script = (
        "import sys; from pathlib import Path; "
        "from teatree.cli.push_gate_tools import _brief_for_worktree; "
        "print(repr(_brief_for_worktree(Path(sys.argv[1]))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "''"


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
    def test_plain_word_constraint_refuses_before_doctest(self) -> None:
        with (
            patch("teatree.cli.push_gate_tools.resolve_plan", return_value=_SCOPED),
            patch("teatree.cli.push_gate_tools._brief_for_worktree", return_value="No migration."),
            patch(
                "teatree.cli.push_gate_tools.run_brief_constraints",
                return_value=(ConstraintViolation("no_migration", "0002.py", "migration changed: 0002.py"),),
            ),
            patch("teatree.cli.push_gate_tools.run_push_gate") as sweep,
        ):
            result = runner.invoke(app, ["tool", "push-gate", "--run"])
        assert result.exit_code == 1
        assert "brief constraint FAILED (no_migration)" in result.output
        sweep.assert_not_called()

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
            patch("teatree.cli.push_gate_tools._brief_for_worktree", return_value=""),
        ):
            result = runner.invoke(app, ["tool", "push-gate", "--run"])
        assert result.exit_code == 0
        assert "ast-grep: CLEAN" in result.output

    def test_run_reports_no_rules_as_a_distinct_failure(self) -> None:
        no_rules = PushGateResult(
            ok=False,
            doctest_ok=True,
            astgrep_findings=(),
            astgrep_deferred=False,
            notes=("FAILED: no blocking rules declared",),
            exit_code=1,
            astgrep_rules_present=False,
        )
        with (
            patch("teatree.cli.push_gate_tools.resolve_plan", return_value=_SCOPED),
            patch("teatree.cli.push_gate_tools.run_push_gate", return_value=no_rules),
            patch("teatree.cli.push_gate_tools._brief_for_worktree", return_value=""),
        ):
            result = runner.invoke(app, ["tool", "push-gate", "--run"])
        assert result.exit_code == 1
        assert "ast-grep: NO_RULES" in result.output

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
            patch("teatree.cli.push_gate_tools._brief_for_worktree", return_value=""),
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
            patch("teatree.cli.push_gate_tools._brief_for_worktree", return_value=""),
        ):
            result = runner.invoke(app, ["tool", "push-gate", "--run"])

        assert result.exit_code == 137
