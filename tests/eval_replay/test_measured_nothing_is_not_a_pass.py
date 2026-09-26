"""A run that graded nothing must not read as a run that graded everything green.

The measured failure: five consecutive periodic local eval runs recorded
``0 passed, 0 failed, 273 skipped (of 273)`` and exited 0. Every scenario skipped
(no recorded transcript on disk), so the suite proved nothing — yet the command
succeeded and the ledger line was shaped exactly like a clean pass, so nothing
downstream could tell the two apart.

The two facts are opposite, and each surface now says which one it is:

*   The COMMAND exits :data:`MEASURED_NOTHING_EXIT_CODE` (75) — the eval lanes'
    existing tolerated-status code, already scoped by ``allow_failure.exit_codes``.
    Tolerated, never green. ``--require-executed`` still escalates the same state to
    a hard 1, so no CI lane loses its red.
*   The LEDGER line says ``MEASURED NOTHING`` instead of a pass-shaped tally, and
    the JSON carries the flag, so ``t3 eval history`` cannot be misread at a glance.

The record is still written: a declined run that vanishes from the ledger is the
same blindness pointing the other way.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from click.testing import Result
from django.test import TestCase
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.eval.history import render_history_json, render_history_text
from teatree.core.models import EvalRunRecord, EvalVerdict
from teatree.eval.models import EvalSpec, Matcher
from teatree.eval.skip_guard import MEASURED_NOTHING_EXIT_CODE

_FIXTURES = Path(__file__).resolve().parents[2] / "evals" / "fixtures"


def _spec(name: str) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(
            Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="git worktree add"),
        ),
        source_path=Path("/tmp/spec.yaml"),
    )


class TestExitStatusSeparatesDeclinedFromPassed(TestCase):
    """``t3 eval run`` on the transcript backend, with and without transcripts on disk."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.transcript_dir = Path(self._tmp.name)

    def _invoke(self, specs: list[EvalSpec], *args: str) -> Result:
        with patch("teatree.cli.eval.app.discover_specs", return_value=specs):
            return CliRunner().invoke(app, ["eval", "run", "--transcript-dir", str(self.transcript_dir), *args])

    def test_all_skipped_exits_the_measured_nothing_code_not_zero(self) -> None:
        result = self._invoke([_spec("alpha"), _spec("beta")], "--no-persist")
        assert result.exit_code == MEASURED_NOTHING_EXIT_CODE, result.output
        assert "MEASURED NOTHING" in result.output

    def test_a_run_that_graded_something_still_exits_zero(self) -> None:
        transcript = (_FIXTURES / "worktree_first_pass.stream.jsonl").read_text(encoding="utf-8")
        (self.transcript_dir / "worktree_first.jsonl").write_text(transcript, encoding="utf-8")
        result = self._invoke([_spec("worktree_first")], "--no-persist")
        assert result.exit_code == 0, result.output
        assert "MEASURED NOTHING" not in result.output

    def test_require_executed_still_hard_reds_rather_than_merely_declining(self) -> None:
        result = self._invoke([_spec("alpha")], "--no-persist", "--require-executed")
        assert result.exit_code == 1, result.output

    def test_declined_run_is_still_recorded_in_the_ledger(self) -> None:
        result = self._invoke([_spec("alpha"), _spec("beta")])
        assert result.exit_code == MEASURED_NOTHING_EXIT_CODE, result.output
        record = EvalRunRecord.objects.latest("started_at")
        assert record.total == 2
        assert record.skipped == 2
        assert "MEASURED NOTHING" in render_history_text([record])


class TestLedgerLineSaysWhichFactItIs(TestCase):
    def _measured_nothing_record(self) -> EvalRunRecord:
        run = EvalRunRecord.objects.record(model="haiku")
        run.record_scenario(scenario_name="alpha", verdict=EvalVerdict.SKIP)
        run.record_scenario(scenario_name="beta", verdict=EvalVerdict.SKIP)
        return run

    def _graded_record(self) -> EvalRunRecord:
        run = EvalRunRecord.objects.record(model="haiku")
        run.record_scenario(scenario_name="alpha", verdict=EvalVerdict.PASS)
        run.record_scenario(scenario_name="beta", verdict=EvalVerdict.SKIP)
        return run

    def test_text_marks_a_run_that_measured_nothing(self) -> None:
        line = render_history_text([self._measured_nothing_record()])
        assert "MEASURED NOTHING" in line
        assert "0 passed, 0 failed" not in line, (
            "a pass-shaped tally on a run that graded nothing is the exact misread this fixes."
        )

    def test_text_leaves_a_graded_run_in_its_pass_shaped_tally(self) -> None:
        line = render_history_text([self._graded_record()])
        assert "MEASURED NOTHING" not in line
        assert "1 passed, 0 failed, 1 skipped (of 2)" in line

    def test_json_carries_the_flag_on_a_run_that_measured_nothing(self) -> None:
        payload = json.loads(render_history_json([self._measured_nothing_record()]))
        assert payload["runs"][0]["measured_nothing"] is True
        assert payload["runs"][0]["executed"] == 0

    def test_json_carries_the_flag_on_a_graded_run(self) -> None:
        payload = json.loads(render_history_json([self._graded_record()]))
        assert payload["runs"][0]["measured_nothing"] is False
        assert payload["runs"][0]["executed"] == 1

    def test_a_suite_with_nothing_to_run_has_not_declined(self) -> None:
        payload = json.loads(render_history_json([EvalRunRecord.objects.record(model="haiku")]))
        assert payload["runs"][0]["measured_nothing"] is False, "a suite with nothing to run has not DECLINED to run."
