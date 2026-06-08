"""Whole-suite HTML report (`t3 eval --html <path>`) — lane table + verdict."""

import contextlib
from pathlib import Path
from unittest.mock import patch

from teatree.cli.eval.all import LaneResult, run_full_suite
from teatree.cli.eval.suite_html import build_suite_verdict, render_suite_html


def _lane(name: str, *, passed: bool, skipped: bool = False, detail: str = "") -> LaneResult:
    return LaneResult(name=name, cost="free", passed=passed, skipped=skipped, detail=detail, duration_s=1.0)


class TestBuildSuiteVerdict:
    def test_all_pass_is_good(self) -> None:
        lanes = [_lane("a", passed=True), _lane("b", passed=True)]
        assert "ALL GOOD" in build_suite_verdict(lanes)

    def test_a_real_fail_is_problems_found_naming_the_lane(self) -> None:
        lanes = [_lane("a", passed=True), _lane("pinned-regressions", passed=False)]
        verdict = build_suite_verdict(lanes)
        assert "PROBLEMS FOUND" in verdict
        assert "pinned-regressions" in verdict

    def test_a_skip_is_noted_not_counted_as_fail(self) -> None:
        lanes = [_lane("a", passed=True), _lane("ai-eval", passed=True, skipped=True, detail="no transcripts")]
        verdict = build_suite_verdict(lanes)
        assert "PROBLEMS FOUND" not in verdict
        assert "ai-eval" in verdict


class TestRenderSuiteHtml:
    def test_every_lane_renders_a_row(self) -> None:
        lanes = [
            _lane("skill-triggers", passed=True, detail="40 checks"),
            LaneResult(name="ai-eval", cost="metered (sdk)", passed=False, skipped=False, detail="2 failed"),
        ]
        html = render_suite_html(lanes)
        assert "skill-triggers" in html
        assert "ai-eval" in html
        assert "metered (sdk)" in html
        assert "2 failed" in html

    def test_duration_is_rendered(self) -> None:
        lane = LaneResult(name="a", cost="free", passed=True, skipped=False, detail="", duration_s=12.5)
        html = render_suite_html([lane])
        assert "12.5" in html

    def test_verdict_banner_is_at_the_top(self) -> None:
        html = render_suite_html([_lane("pinned-regressions", passed=False)])
        assert "PROBLEMS FOUND" in html
        assert html.index("PROBLEMS FOUND") < html.index("<table")

    def test_values_are_html_escaped(self) -> None:
        html = render_suite_html([_lane("x", passed=False, detail="<script>alert(1)</script>")])
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_fail_lane_marked_red_pass_green(self) -> None:
        html = render_suite_html([_lane("good", passed=True), _lane("bad", passed=False)])
        assert "fail" in html.lower()
        assert "pass" in html.lower()

    def test_is_self_contained_no_external_assets(self) -> None:
        html = render_suite_html([_lane("a", passed=True)])
        assert "<style>" in html
        assert "http://" not in html
        assert "https://" not in html


class TestHtmlPathOption:
    def test_html_flag_writes_the_file(self, tmp_path: Path) -> None:
        out = tmp_path / "report.html"
        with (
            patch("teatree.cli.eval.all.run_trigger_qa", return_value=object()),
            patch("teatree.cli.eval.all.run_regression_corpus"),
            patch("teatree.cli.eval.all.run_negative_control"),
            patch("teatree.cli.eval.all.skill_eval_coverage"),
            patch("teatree.cli.eval.all.replay_transcript_for_all", return_value=None),
            patch("teatree.cli.eval.all.ensure_django"),
            patch("teatree.cli.eval.all.trigger_lane", return_value=_lane("skill-triggers", passed=True, detail="ok")),
            patch("teatree.cli.eval.all.coverage_lane", return_value=_lane("skill-coverage", passed=True)),
            patch("teatree.cli.eval.all.regression_lane", return_value=_lane("pinned-regressions", passed=True)),
            patch("teatree.cli.eval.all.negative_control_lane", return_value=_lane("negative-control", passed=True)),
            patch(
                "teatree.cli.eval.all.transcript_replay_lane",
                return_value=_lane("transcript-replay", passed=True, skipped=True),
            ),
            contextlib.suppress(SystemExit),
        ):
            run_full_suite(backend="subscription", transcript_dir=None, free_only=True, docker=False, html_path=out)
        assert out.is_file()
        body = out.read_text(encoding="utf-8")
        assert "skill-triggers" in body
        assert "<table" in body
