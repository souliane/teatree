"""#550 Tier-3 lane: ``t3 eval skill-prose-judge`` — model-judged, ADVISORY.

The lane scores each SKILL.md's prose via the existing ``ClaudeJudge`` seam
(mocked here — no metered call) and is ADVISORY: it renders scores + nominates
the weakest skill, but a low score NEVER fails the lane / exits non-zero. The
judge boundary is mocked in every test; the live metered run happens through the
metered lane, not unit tests.
"""

from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.eval.skill_prose_lane import skill_prose_judge_lane
from teatree.eval.skill_prose_judge import ProseJudgeReport, ProseScore


def _report(*scores: ProseScore, skipped: int = 0) -> ProseJudgeReport:
    return ProseJudgeReport(scores=tuple(scores), skipped=skipped)


class TestLaneResult:
    def test_lane_is_advisory_and_always_passes_even_on_low_scores(self) -> None:
        report = _report(ProseScore("weak", 0.0, "bad prose"), ProseScore("ok", 0.9, "good"))
        lane = skill_prose_judge_lane(report)
        assert lane.name == "skill-prose-judge"
        # ADVISORY: a 0.0 score does NOT fail the lane / suite
        assert lane.passed is True
        assert "advisory" in lane.cost.lower() or "advisory" in lane.detail.lower()

    def test_lane_reports_the_nominated_weakest_skill(self) -> None:
        report = _report(ProseScore("weak", 0.1, "x"), ProseScore("strong", 0.9, "y"))
        lane = skill_prose_judge_lane(report)
        assert "weak" in lane.detail

    def test_all_skipped_judge_is_a_skip_not_a_fail(self) -> None:
        report = _report(ProseScore("x", None, "judge skipped"), skipped=1)
        lane = skill_prose_judge_lane(report)
        assert lane.passed is True
        assert lane.skipped is True


class TestStandaloneCommand:
    def test_subcommand_runs_with_a_mocked_judge_and_exits_zero(self) -> None:
        report = _report(ProseScore("weak", 0.0, "advisory only"))
        with patch("teatree.cli.eval.skill_prose_lane.run_prose_judge", return_value=report):
            result = CliRunner().invoke(app, ["eval", "skill-prose-judge"])
        # ADVISORY: even a 0.0 score exits 0 — the lane never gates CI
        assert result.exit_code == 0, result.output
        assert "weak" in result.output

    def test_low_score_never_exits_nonzero(self) -> None:
        report = _report(ProseScore("a", 0.0, "x"), ProseScore("b", 0.0, "y"))
        with patch("teatree.cli.eval.skill_prose_lane.run_prose_judge", return_value=report):
            result = CliRunner().invoke(app, ["eval", "skill-prose-judge"])
        assert result.exit_code == 0, result.output

    def test_json_format_is_accepted(self) -> None:
        report = _report(ProseScore("a", 0.5, "mid"))
        with patch("teatree.cli.eval.skill_prose_lane.run_prose_judge", return_value=report):
            result = CliRunner().invoke(app, ["eval", "skill-prose-judge", "--format", "json"])
        assert result.exit_code == 0, result.output
        assert '"advisory"' in result.output
